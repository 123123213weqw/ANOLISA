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

"""Patch extraction policy for SWE-bench submissions."""

from __future__ import annotations

import re

# ``.git/info/exclude`` rules installed into prepared repositories before an
# agent runs. This prevents common tooling artefacts from being staged by the
# final ``git add -A`` while keeping the upstream repository untouched.
REPO_EXCLUDE_RULES = (
    "# swe-runner: keep tooling side-effects out of the patch",
    ".openclaw/",
    ".runner/",
    "AGENTS.md",
    ".local/",
    "**/__pycache__/",
    "*.pyc",
    "*.pyo",
    "*.egg-info/",
    "build/",
    "**/build/lib/",
    "*.mo",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
)

# Git pathspec excludes applied when generating the final ``git diff``.
# These paths are known to break SWE-bench's ``patch`` apply step:
#   * ``*.mo`` / ``*.db`` / ``*.sqlite*`` produce binary diffs.
#   * ``build/**`` / ``**/build/lib/**`` contain duplicated generated trees.
#   * Tests and docs are useful during exploration but should not be submitted.
#   * Project/build configuration exclusions match the current benchmark scope.
#   * Tooling side effects such as ``.openclaw`` and ``AGENTS.md`` are never fixes.
PATCH_DIFF_EXCLUDE_PATHSPECS: tuple[str, ...] = (
    # Test suites and test helper files.
    ":(exclude,glob)tests/**",
    ":(exclude,glob)test/**",
    ":(exclude,glob)testing/**",
    ":(exclude,glob)**/tests/**",
    ":(exclude,glob)**/testing/**",
    ":(exclude,glob)**/test_*.py",
    ":(exclude,glob)**/conftest.py",
    # Documentation and generated reports.
    ":(exclude,glob)docs/**",
    ":(exclude,glob)doc/**",
    ":(exclude,glob)**/*.md",
    ":(exclude,glob)**/*.rst",
    ":(exclude,glob)htmlcov/**",
    ":(exclude,glob)coverage.xml",
    ":(exclude,glob).coverage",
    ":(exclude,glob).coverage.*",
    # Patch artifacts commonly left by agents.
    ":(exclude,glob)*.patch",
    ":(exclude,glob)*.diff",
    ":(exclude,glob)*.rej",
    ":(exclude,glob)*.orig",
    ":(exclude,glob)*.tmp",
    ":(exclude,glob)*.log",
    # Project and build configuration files. See benchmark-scope note above.
    ":(exclude,glob)pyproject.toml",
    ":(exclude,glob)setup.py",
    ":(exclude,glob)setup.cfg",
    ":(exclude,glob)tox.ini",
    ":(exclude,glob)requirements*.txt",
    ":(exclude,glob)requirements/**",
    ":(exclude,glob)Pipfile",
    ":(exclude,glob)Pipfile.lock",
    ":(exclude,glob)poetry.lock",
    ":(exclude,glob)environment.yml",
    ":(exclude,glob)environment.yaml",
    ":(exclude,glob)package.json",
    ":(exclude,glob)package-lock.json",
    ":(exclude,glob)yarn.lock",
    ":(exclude,glob)pnpm-lock.yaml",
    ":(exclude,glob)Makefile",
    ":(exclude,glob)Dockerfile",
    ":(exclude,glob)docker-compose*.yml",
    ":(exclude,glob)docker-compose*.yaml",
    ":(exclude,glob)CMakeLists.txt",
    ":(exclude,glob)cmake/**",
    ":(exclude,glob)build.gradle",
    ":(exclude,glob)gradle.properties",
    ":(exclude,glob)webpack*.js",
    ":(exclude,glob)gulpfile*.js",
    # Build artefacts, dependency directories, and tool caches.
    ":(exclude,glob)**/*.mo",
    ":(exclude,glob)**/*.pyc",
    ":(exclude,glob)**/__pycache__/**",
    ":(exclude,glob)**/*.db",
    ":(exclude,glob)**/*.sqlite",
    ":(exclude,glob)**/*.sqlite3",
    ":(exclude,glob)build/**",
    ":(exclude,glob)**/build/lib/**",
    ":(exclude,glob)dist/**",
    ":(exclude,glob)**/*.egg-info/**",
    ":(exclude,glob).eggs/**",
    ":(exclude,glob)**/node_modules/**",
    ":(exclude,glob).pytest_cache/**",
    ":(exclude,glob).hypothesis/**",
    ":(exclude,glob).mypy_cache/**",
    ":(exclude,glob).ruff_cache/**",
    ":(exclude,glob).openclaw/**",
    ":(exclude,glob)AGENTS.md",
)

_BINARY_MARKER_RE = re.compile(r"(?m)^Binary files .+ differ\s*$")
_DIFF_HEADER_RE = re.compile(r"(?m)^diff --git a/(.*?) b/(.*?)$")
# Git C-quotes both sides of the header when a path contains non-ASCII bytes
# (``core.quotePath`` enabled) or control/quote characters, e.g.
# ``diff --git "a/verify_\345\267\245.py" "b/verify_\345\267\245.py"``.
_QUOTED_DIFF_HEADER_RE = re.compile(
    r'(?m)^diff --git "a/((?:[^"\\]|\\.)*)" "b/((?:[^"\\]|\\.)*)"$'
)
_HUNK_RE = re.compile(r"(?m)^@@ ")
_NEW_FILE_RE = re.compile(r"(?m)^new file mode ")

# C-style escapes emitted by Git's path quoting. Unrecognised bytes and every
# octal escape are raw byte values, so quoted paths are decoded as bytes and
# only then interpreted as text.
_GIT_C_ESCAPES = {
    "a": b"\a",
    "b": b"\b",
    "f": b"\f",
    "n": b"\n",
    "r": b"\r",
    "t": b"\t",
    "v": b"\v",
    "\\": b"\\",
    '"': b'"',
}
_ROOT_HELPER_NAMES: frozenset[str] = frozenset(
    {
        "debug.py",
        "repro.py",
        "reproduce.py",
        "run_test.py",
        "run_tests.py",
        "test_fix.py",
        "verify_fix.py",
    }
)


def clean_model_patch_diff(diff_text: str) -> str | None:
    """Apply final SWE-bench patch policy to raw ``git diff`` text."""
    cleaned = _strip_binary_only_sections(diff_text) if diff_text.strip() else ""
    cleaned = _strip_new_root_helper_sections(cleaned) if cleaned.strip() else ""
    if not cleaned.strip():
        return None
    return cleaned if cleaned.endswith("\n") else cleaned + "\n"


def _strip_binary_only_sections(diff_text: str) -> str:
    """Drop ``diff --git`` sections that only contain a binary marker."""
    parts = re.split(r"(?m)^(?=diff --git )", diff_text)
    kept: list[str] = []
    for part in parts:
        if not part.strip():
            continue
        if _BINARY_MARKER_RE.search(part) and not _HUNK_RE.search(part):
            continue
        kept.append(part)
    return "".join(kept)


def _is_root_helper_path(path: str) -> bool:
    """Return true for common ad-hoc root-level verification script names."""
    if "/" in path:
        return False
    return (
        path in _ROOT_HELPER_NAMES
        or (path.startswith("verify_") and path.endswith(".py"))
        or (path.startswith("test_") and path.endswith(".py"))
    )


def _decode_git_c_quoted(quoted: str) -> str:
    """Decode one side of a Git C-quoted diff header path."""
    raw = bytearray()
    i = 0
    while i < len(quoted):
        ch = quoted[i]
        if ch != "\\":
            raw.extend(ch.encode("utf-8", "surrogateescape"))
            i += 1
            continue
        i += 1
        if i >= len(quoted):
            raw += b"\\"
            break
        esc = quoted[i]
        mapped = _GIT_C_ESCAPES.get(esc)
        if mapped is not None:
            raw += mapped
            i += 1
            continue
        digits = ""
        while i < len(quoted) and len(digits) < 3 and quoted[i] in "01234567":
            digits += quoted[i]
            i += 1
        if digits:
            raw.append(int(digits, 8) & 0xFF)
            continue
        raw.extend(esc.encode("utf-8", "surrogateescape"))
        i += 1
    return raw.decode("utf-8", "replace")


def _diff_header_destination(part: str) -> str | None:
    """Return the destination path of a ``diff --git`` section, if readable."""
    quoted = _QUOTED_DIFF_HEADER_RE.search(part)
    if quoted:
        return _decode_git_c_quoted(quoted.group(2))
    header = _DIFF_HEADER_RE.search(part)
    if header:
        return header.group(2)
    return None


def _strip_new_root_helper_sections(diff_text: str) -> str:
    """Drop newly-added root helper scripts while preserving tracked files."""
    parts = re.split(r"(?m)^(?=diff --git )", diff_text)
    kept: list[str] = []
    for part in parts:
        if not part.strip():
            continue
        destination = _diff_header_destination(part)
        if (
            destination
            and _NEW_FILE_RE.search(part)
            and _is_root_helper_path(destination)
        ):
            continue
        kept.append(part)
    return "".join(kept)
