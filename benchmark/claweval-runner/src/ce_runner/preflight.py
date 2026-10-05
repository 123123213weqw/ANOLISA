# Copyright 2026 Alibaba Cloud
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Pre-flight environment checks run before a batch starts.

These guard against intermittent production issues where the local
``openclaw`` install is broken (e.g. a plugin fails to load because of a
mangled ``node_modules`` tree) or the Docker daemon is unreachable. Either
condition makes every task run fail in confusing ways, so we detect them up
front and abort with an actionable message instead of burning a whole batch.

Design notes:
  - ``openclaw plugins doctor`` exit code is unreliable, so we parse its
    output for positive error signals. We only fail on an explicit error
    marker — never merely because a "success" string is absent — to avoid
    false positives that would needlessly block a healthy run.
  - We always check ``docker info`` because ce-runner now runs in
    always-sandbox mode, so Docker is unconditionally required.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

# Matches a per-plugin failure line from ``openclaw plugins doctor``, e.g.
#   - lmstudio [load]: No "exports" main defined in .../package.json
#   - foo [init]: boom
_PLUGIN_ERROR_RE = re.compile(r"^- (\S+) \[(load|init)\]: (.+)$", re.MULTILINE)

# Substrings that, if present in doctor output, indicate a broken install.
_FATAL_MARKERS = (
    "Plugin errors:",
    "Failed to start CLI:",
    "PluginLoadFailureError",
)

_DOCTOR_TIMEOUT = 30
_DOCKER_TIMEOUT = 15


def _decode_probe_output(data: bytes | str | None) -> str:
    """Decode captured probe output as UTF-8 with universal newlines.

    ``text=True`` would raise ``UnicodeDecodeError`` from inside
    ``communicate()`` (or corrupt text-reader threads), so probes capture
    bytes and decode explicitly; undecodable output becomes an actionable
    error instead of an aborted preflight.
    """
    if data is None:
        return ""
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _run_required_probe(
    command: list[str],
    timeout: int,
    missing_hint: str,
) -> tuple[subprocess.CompletedProcess[str] | None, list[str]]:
    """Run one required probe, capturing bytes and decoding as UTF-8.

    Returns ``(completed, [])`` when the command ran and its output decoded,
    or ``(None, errors)`` with an actionable message when it could not be
    launched, timed out, or emitted undecodable output.
    """
    label = " ".join(command)
    try:
        proc = subprocess.run(command, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return None, [missing_hint]
    except subprocess.TimeoutExpired:
        return None, [f"'{label}' timed out after {timeout}s"]
    except OSError as exc:
        return None, [f"'{label}' failed to launch: {exc}"]

    try:
        stdout = _decode_probe_output(proc.stdout)
        stderr = _decode_probe_output(proc.stderr)
    except UnicodeDecodeError:
        return None, [f"'{label}' output was not valid UTF-8"]
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr), []


def check_openclaw_plugins() -> list[str]:
    """Run ``openclaw plugins doctor`` and report plugin load failures.

    Returns a list of human-readable error strings (empty when healthy).
    """
    proc, launch_errors = _run_required_probe(
        ["openclaw", "plugins", "doctor"],
        _DOCTOR_TIMEOUT,
        "'openclaw' command not found (is openclaw installed?)",
    )
    if proc is None:
        return launch_errors

    output = proc.stdout + "\n" + proc.stderr

    errors: list[str] = []
    for plugin, phase, msg in _PLUGIN_ERROR_RE.findall(output):
        errors.append(f"plugin '{plugin}' failed to {phase}: {msg.strip()}")

    if not errors:
        # No structured plugin lines parsed, but a fatal marker may still
        # signal a broken install (e.g. CLI failed to start at all).
        for marker in _FATAL_MARKERS:
            if marker in output:
                errors.append(f"openclaw reported '{marker.rstrip(':')}'")
                break

    return errors


def check_docker() -> list[str]:
    """Run ``docker info`` and report failures via exit code.

    Returns a list of human-readable error strings (empty when healthy).
    """
    proc, launch_errors = _run_required_probe(
        ["docker", "info"],
        _DOCKER_TIMEOUT,
        "'docker' command not found (is Docker installed?)",
    )
    if proc is None:
        return launch_errors

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit code {proc.returncode}"
        return [f"docker daemon not reachable: {tail}"]

    return []


def run_preflight_checks() -> tuple[bool, list[str]]:
    """Run all pre-flight environment checks.

    Returns ``(ok, errors)`` where ``ok`` is True when the environment is
    healthy and ``errors`` is the aggregated list of problems found.
    """
    errors: list[str] = []
    errors.extend(check_openclaw_plugins())
    errors.extend(check_docker())
    return (not errors, errors)


def _resolve_project_root(task_dir: Path) -> Path:
    """Find the nearest ancestor of *task_dir* containing a ``tasks/`` subdir.

    Mirrors the project-root walk in
    ``SandboxRunner._inject_file_list`` so that cross-task fixture references
    (paths relative to the claw-eval root) resolve identically here. Falls
    back to the task dir's parent when no ``tasks/`` ancestor is found.
    """
    resolved = task_dir.resolve()
    project_root = resolved.parent  # fallback
    cur = resolved
    while cur.parent != cur:
        if (cur / "tasks").is_dir():
            return cur
        cur = cur.parent
    return project_root


def find_missing_fixtures(task_dirs: list[str]) -> dict[str, list[str]]:
    """Report declared ``sandbox_files`` that cannot be resolved on disk.

    For each task directory, resolves every declared sandbox file the same
    way ``SandboxRunner.inject_files`` does: first relative to the task dir,
    then relative to the project root (the nearest ``tasks/`` ancestor). Also
    honours the legacy ``environment.fixtures`` fallback.

    Returns a mapping ``{task_yaml_path: [missing_rel_paths]}`` containing
    only tasks that have at least one unresolvable file. Binary fixtures such
    as videos are not shipped in git, so a missing entry typically means the
    fixture archive has not been downloaded.
    """
    from ._common import load_task_yaml

    missing: dict[str, list[str]] = {}
    for td in task_dirs:
        task_yaml = os.path.join(td, "task.yaml")
        try:
            ty = load_task_yaml(task_yaml)
        except Exception:
            continue

        file_list = ty.get("sandbox_files") or []
        if not file_list:
            env = ty.get("environment") or {}
            file_list = env.get("fixtures") or []
        if not file_list:
            continue

        task_root = Path(td)
        project_root = _resolve_project_root(task_root)
        not_found: list[str] = []
        for rel_path in file_list:
            if (task_root / rel_path).exists():
                continue
            if (project_root / rel_path).exists():
                continue
            not_found.append(rel_path)

        if not_found:
            missing[task_yaml] = not_found

    return missing
