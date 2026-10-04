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

"""Tests for shared command execution helpers."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from swe_runner.common.commands import run_command


def test_run_command_normalizes_text_output() -> None:
    proc = MagicMock()
    proc.communicate.return_value = ("out", None)
    proc.returncode = 0

    with patch("swe_runner.common.commands.subprocess.Popen", return_value=proc) as mock_popen:
        result = run_command(["echo", "hello"], cwd=Path("/tmp/work"), timeout=5)

    assert result.args == ("echo", "hello")
    assert result.returncode == 0
    assert result.stdout == "out"
    assert result.stderr == ""
    assert result.output == "out"
    kwargs = mock_popen.call_args.kwargs
    assert kwargs["cwd"] == "/tmp/work"
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    assert kwargs["text"] is True
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert kwargs["start_new_session"] is True


def test_run_command_preserves_subprocess_exceptions() -> None:
    proc = MagicMock()
    proc.pid = 12345
    proc.communicate.side_effect = subprocess.TimeoutExpired(cmd=["sleep"], timeout=1)
    proc.wait.return_value = None

    with (
        patch("swe_runner.common.commands.subprocess.Popen", return_value=proc),
        patch("swe_runner.common.commands.os.getpgid", return_value=12345),
        patch("swe_runner.common.commands.os.killpg") as killpg,
    ):
        try:
            run_command(["sleep"], timeout=1)
        except subprocess.TimeoutExpired as exc:
            assert exc.timeout == 1
        else:
            raise AssertionError("Expected TimeoutExpired")

    killpg.assert_called_with(12345, signal.SIGTERM)


GRANDCHILD_CODE = """
import os, signal, sys, time
marker = sys.argv[1]
signal.signal(signal.SIGTERM, signal.SIG_DFL)
with open(marker, "a") as f:
    f.write(f"PID {os.getpid()}\\n")
    f.flush()
    while True:
        f.write("x\\n")
        f.flush()
        time.sleep(0.05)
"""

CHILD_CODE = """
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", {grandchild!r}, sys.argv[1]])
time.sleep(60)
"""


def _kill_leftover_grandchild(marker: Path) -> None:
    """Best-effort cleanup so a failing (pre-fix) run leaves no orphan."""
    try:
        first = marker.read_text().splitlines()[0]
    except (OSError, IndexError):
        return
    if first.startswith("PID "):
        with contextlib.suppress(ProcessLookupError, ValueError, PermissionError):
            os.kill(int(first.split()[1]), signal.SIGKILL)


def test_run_command_timeout_kills_grandchildren(tmp_path: Path) -> None:
    """A timeout must kill the whole process tree, not only the leaf.

    The child spawns a grandchild that keeps appending to a marker file;
    after run_command times out, the grandchild must be dead and the marker
    must stop growing (while proving it did run).
    """
    marker = tmp_path / "grandchild_writes.txt"
    child_code = textwrap.dedent(CHILD_CODE).format(grandchild=textwrap.dedent(GRANDCHILD_CODE))

    try:
        with pytest.raises(subprocess.TimeoutExpired):
            run_command([sys.executable, "-c", child_code, str(marker)], timeout=1.0)

        deadline = time.monotonic() + 5.0
        first_line = ""
        while time.monotonic() < deadline:
            if marker.exists():
                content = marker.read_text()
                if content.startswith("PID "):
                    first_line = content.splitlines()[0]
                    break
            time.sleep(0.1)
        assert first_line, "grandchild never wrote its marker file"
        grandchild_pid = int(first_line.split()[1])

        # The grandchild may take a moment to die after the group kill.
        for _ in range(50):
            try:
                os.kill(grandchild_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            raise AssertionError(
                f"grandchild pid {grandchild_pid} survived the timeout kill")

        size = marker.stat().st_size
        assert size > 0, "grandchild ran but wrote nothing"
        time.sleep(0.5)
        assert marker.stat().st_size == size, (
            "marker file kept growing after run_command timed out — "
            "an orphaned grandchild is still writing")
    finally:
        _kill_leftover_grandchild(marker)
