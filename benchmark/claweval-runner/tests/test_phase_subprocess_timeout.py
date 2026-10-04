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

"""Test convert/grade phase subprocess timeouts.

``phase_convert`` / ``phase_grade`` ran their subprocesses without a
timeout: a hung converter or grader (e.g. blocked on a network fetch with
no internal timeout) occupied a grade-pool worker forever, and batch
mode's ``while grade_futures`` drain loop never terminated — the whole
batch hung. Both phases now carry a wall-clock budget
(``DEFAULT_PHASE_TIMEOUT_S``, overridable per call); on timeout the trial
is classified as errored instead of hanging.

The hang tests redirect ``_PYTHON`` at a stub interpreter that sleeps
forever; only pids spawned by these tests are involved.
"""

import stat
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

HANG_STUB = "#!/bin/sh\nexec sleep 600\n"
# Fast stub: create the file passed after --output, then exit 0 (mimics a
# healthy converter subprocess).
FAST_CONVERT_STUB = (
    "#!/bin/sh\n"
    "while [ $# -gt 0 ]; do\n"
    "  if [ \"$1\" = \"--output\" ]; then touch \"$2\"; fi\n"
    "  shift\n"
    "done\n"
    "exit 0\n"
)
FAST_GRADE_STUB = "#!/bin/sh\nexit 0\n"


def _write_stub(tmp_path, name, body):
    stub = tmp_path / name
    stub.write_text(body)
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return str(stub)


def _phase_fixture(tmp_path):
    session_file = tmp_path / "session.jsonl"
    session_file.write_text('{"type": "message"}\n')
    task_yaml = tmp_path / "task.yaml"
    task_yaml.write_text("task_id: T001\nservices: []\ntools: []\n")
    output_file = tmp_path / "output.jsonl"
    return str(session_file), str(task_yaml), str(output_file)


class TestPhaseConvertTimeout:
    """phase_convert: a hung converter returns False within the budget."""

    def test_hung_converter_returns_false_within_timeout(self, tmp_path):
        from ce_runner import pipeline

        stub = _write_stub(tmp_path, "hang_python.sh", HANG_STUB)
        session_file, task_yaml, output_file = _phase_fixture(tmp_path)

        start = time.monotonic()
        with patch.object(pipeline, "_PYTHON", stub):
            ok = pipeline.phase_convert(session_file, task_yaml,
                                        output_file, timeout=2)
        elapsed = time.monotonic() - start

        assert ok is False
        assert elapsed < 30, f"timeout did not fire (took {elapsed:.1f}s)"

    def test_default_timeout_forwarded_to_subprocess(self, tmp_path):
        """Without an explicit timeout, the generous default is passed to
        subprocess.run (previously no timeout was passed at all)."""
        from ce_runner import pipeline

        session_file, task_yaml, output_file = _phase_fixture(tmp_path)
        mock_result = MagicMock()
        mock_result.returncode = 1

        with patch("ce_runner.pipeline.subprocess.run",
                   return_value=mock_result) as mock_run:
            pipeline.phase_convert(session_file, task_yaml, output_file)

        assert mock_run.call_args.kwargs.get("timeout") == \
            pipeline.DEFAULT_PHASE_TIMEOUT_S

    def test_fast_converter_unaffected(self, tmp_path):
        """A healthy converter finishing inside the budget still succeeds."""
        from ce_runner import pipeline

        stub = _write_stub(tmp_path, "fast_convert.sh", FAST_CONVERT_STUB)
        session_file, task_yaml, output_file = _phase_fixture(tmp_path)

        with patch.object(pipeline, "_PYTHON", stub):
            ok = pipeline.phase_convert(session_file, task_yaml,
                                        output_file, timeout=30)

        assert ok is True


class TestPhaseGradeTimeout:
    """phase_grade: a hung grader becomes an errored trial within budget."""

    def test_hung_grader_returns_errored_scores(self, tmp_path):
        from ce_runner import pipeline

        stub = _write_stub(tmp_path, "hang_python.sh", HANG_STUB)
        trace_file = tmp_path / "trace.jsonl"
        trace_file.write_text('{"type": "trace_start"}\n')
        task_yaml = tmp_path / "task.yaml"
        task_yaml.write_text("task_id: T001\nservices: []\ntools: []\n")
        judge = {"model": "m", "base_url": "http://x", "api_key": "k"}

        start = time.monotonic()
        with patch.object(pipeline, "_PYTHON", stub):
            scores = pipeline.phase_grade(str(trace_file), str(task_yaml),
                                          judge, timeout=2)
        elapsed = time.monotonic() - start

        assert elapsed < 30, f"timeout did not fire (took {elapsed:.1f}s)"
        # Errored-trial shape: zero scores + error marker (the batch
        # aggregation excludes error trials from avg_score).
        assert scores["task_score"] == 0.0
        assert scores["passed"] is False
        assert "timed out" in scores["error"]

    def test_fast_grader_unaffected(self, tmp_path):
        """A healthy grader finishing inside the budget returns the plain
        score dict with no error marker."""
        from ce_runner import pipeline

        stub = _write_stub(tmp_path, "fast_grade.sh", FAST_GRADE_STUB)
        trace_file = tmp_path / "trace.jsonl"
        trace_file.write_text('{"type": "trace_start"}\n')
        task_yaml = tmp_path / "task.yaml"
        task_yaml.write_text("task_id: T001\nservices: []\ntools: []\n")
        judge = {"model": "m", "base_url": "http://x", "api_key": "k"}

        with patch.object(pipeline, "_PYTHON", stub):
            scores = pipeline.phase_grade(str(trace_file), str(task_yaml),
                                          judge, timeout=30)

        assert "error" not in scores
        assert scores == {
            "completion": 0.0, "robustness": 0.0, "communication": 0.0,
            "safety": 0.0, "task_score": 0.0, "passed": False,
        }

    def test_default_timeout_forwarded_to_subprocess(self, tmp_path):
        from ce_runner import pipeline

        trace_file = tmp_path / "trace.jsonl"
        trace_file.write_text('{"type": "trace_start"}\n')
        task_yaml = tmp_path / "task.yaml"
        task_yaml.write_text("task_id: T001\nservices: []\ntools: []\n")
        judge = {"model": "m", "base_url": "http://x", "api_key": "k"}
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("ce_runner.pipeline.subprocess.run",
                   return_value=mock_result) as mock_run, \
             patch("ce_runner.pipeline.os.path.getsize", return_value=0), \
             patch("ce_runner.pipeline.os.remove"):
            pipeline.phase_grade(str(trace_file), str(task_yaml), judge)

        assert mock_run.call_args.kwargs.get("timeout") == \
            pipeline.DEFAULT_PHASE_TIMEOUT_S
