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

"""Shared command execution helpers."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandResult:
    """Normalized result from an external command."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        """Combined stdout and stderr."""
        return self.stdout + self.stderr


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    """Best-effort SIGKILL of the process group led by ``proc``.

    Commands are started with ``start_new_session=True`` so the child leads
    its own process group and the group can only be one we created; killing
    it reaps grandchildren (CLI workers, shells, sandbox helpers) that a
    kill of the direct child alone would orphan.  ProcessLookupError and
    OSError are ignored because the group may already be gone.
    """
    with contextlib.suppress(ProcessLookupError, OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


def _terminate_process_group(proc: subprocess.Popen[str]) -> None:
    """Terminate the whole process group: SIGTERM, grace, then SIGKILL."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        proc.wait()
        return
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        proc.wait()


def run_command(
    args: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout: float | None = None,
    check: bool = False,
    encoding: str = "utf-8",
    errors: str = "replace",
) -> CommandResult:
    """Run a text command and return a normalized result.

    The command is started in its own session so that a timeout terminates
    the entire process tree instead of only the direct child — orphaned
    grandchildren would otherwise keep writing to the mounted workspace
    while the instance is failed, patch-extracted and cleaned up.

    ``subprocess`` exceptions are intentionally preserved so callers can map
    them to their own domain errors.
    """
    normalized_args = tuple(args)
    proc = subprocess.Popen(
        list(normalized_args),
        cwd=str(cwd) if cwd is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding=encoding,
        errors=errors,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        raise
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, normalized_args, output=stdout, stderr=stderr)
    return CommandResult(
        args=normalized_args,
        returncode=proc.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
    )
