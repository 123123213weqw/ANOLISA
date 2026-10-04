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

"""Trial tables must join producer-written fields.

The batch producer (``batch_runner._collect_completed``) writes each trial
with a ``trace_file`` key plus token/time usage lifted from the trace's
``trace_end`` event.  The summary scripts (``summarize_results.py`` /
``analyze.py``) must read those exact keys so the Trial ID, Failure Reason
and token/time columns are populated for real runs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

TASK_ID = "M001_clock"
TRIAL_HASH = "8a61ea06"
TRACE_NAME = f"{TASK_ID}_{TRIAL_HASH}.jsonl"

TRACE_EVENTS = [
    {
        "type": "trace_start",
        "trace_id": "trace-1",
        "task_id": TASK_ID,
        "timestamp": "2026-01-01T00:00:00+00:00",
    },
    {
        "type": "message",
        "role": "assistant",
        "content": "setting the clock",
        "timestamp": "2026-01-01T00:03:00+00:00",
    },
    {
        "type": "trace_end",
        "trace_id": "trace-1",
        "total_turns": 12,
        "input_tokens": 34567,
        "output_tokens": 1234,
        "total_tokens": 35801,
        "model_time_s": 380.5,
        "tool_time_s": 50.7,
        "other_time_s": 0.0,
        "wall_time_s": 431.2,
        "scores": {
            "completion": 0.0,
            "robustness": 0.0,
            "communication": 0.0,
            "safety": 1.0,
            "efficiency_turns": 12,
            "efficiency_tokens": 35801,
            "efficiency_wall_time_s": 431.2,
        },
        "task_score": 0.0,
        "passed": False,
    },
    {
        "type": "grading_result",
        "trace_id": "trace-1",
        "task_id": TASK_ID,
        "scores": {
            "completion": 0.4,
            "robustness": 0.8,
            "communication": 0.9,
            "safety": 1.0,
            "efficiency_turns": 0.0,
            "efficiency_tokens": 0.0,
            "efficiency_wall_time_s": 0.0,
        },
        "task_score": 0.4,
        "passed": False,
    },
]

JUDGE_CONFIG = {"model": "judge-m", "base_url": "http://judge", "api_key": "k"}


def _write_trace(trace_dir: Path) -> Path:
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_path = trace_dir / TRACE_NAME
    with open(trace_path, "w") as f:
        for event in TRACE_EVENTS:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return trace_path


USAGE_FIELDS = ("input_tokens", "output_tokens", "model_time_s",
                "tool_time_s", "other_time_s")


def _graded_result(trace_path: Path, tmp_path: Path) -> dict:
    """Run the producer's grade phase (grader subprocess stubbed) on a real trace."""
    from ce_runner import pipeline

    fake_run = Mock()
    fake_run.return_value = Mock(returncode=0)
    orig_run = pipeline.subprocess.run
    pipeline.subprocess.run = fake_run
    try:
        scores = pipeline.phase_grade(
            str(trace_path), str(tmp_path / "unused_task.yaml"), JUDGE_CONFIG)
    finally:
        pipeline.subprocess.run = orig_run

    result = dict(scores)
    result["trace_file"] = str(trace_path)
    return result


def _trial_entry(tmp_path: Path) -> dict:
    """Produce the trial entry exactly as the batch producer assembles it.

    Falls back to the pre-fix producer schema (trace_file present, usage
    fields absent) so consumer tests exercise the reader bug itself rather
    than an ImportError.
    """
    trace_path = _write_trace(tmp_path / "traces")
    result = _graded_result(trace_path, tmp_path)
    # _collect_completed stamps grading wall time onto the result before
    # assembling the entry; mirror that here.
    result["wall_time_s"] = 431.2
    try:
        from ce_runner.batch_runner import build_trial_entry
    except ImportError:
        result = {k: v for k, v in result.items() if k not in USAGE_FIELDS}
        return {
            "trial": 1,
            "task_score": result["task_score"],
            "passed": result["passed"],
            "completion": result["completion"],
            "robustness": result["robustness"],
            "communication": result["communication"],
            "safety": result["safety"],
            "error": result.get("error"),
            "wall_time_s": 431.2,
            "session_id": "sess-1",
            "trace_file": result.get("trace_file"),
            "session_archive_file": None,
            "session_origin_file": None,
        }
    return build_trial_entry(1, result, session_id="sess-1")


def _batch_data(entry: dict) -> list:
    return [{
        "task_id": TASK_ID,
        "task_name": "Clock",
        "difficulty": "medium",
        "trials": [entry],
        "error": None,
        "avg_score": entry["task_score"],
        "pass_at_1": 0.0,
        "pass_hat_k": 0.0,
        "avg_passed": False,
    }]


def _write_reports(reports_dir: Path) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "trace_file": TRACE_NAME,
        "status": "fail",
        "failure_classification": {
            "category": "tool_loop",
            "key_reason_zh": "agent 在同一工具上循环",
        },
    }
    with open(reports_dir / f"{TASK_ID}_{TRIAL_HASH}.json", "w") as f:
        json.dump(report, f)


# ── Producer side ──────────────────────────────────────────────────────────


def test_phase_grade_lifts_trace_end_usage_fields(tmp_path: Path) -> None:
    trace_path = _write_trace(tmp_path / "traces")
    result = _graded_result(trace_path, tmp_path)

    assert result["input_tokens"] == 34567
    assert result["output_tokens"] == 1234
    assert result["model_time_s"] == 380.5
    assert result["tool_time_s"] == 50.7
    assert result["other_time_s"] == 0.0
    # Grading scores still come from the grading_result event.
    assert result["task_score"] == pytest.approx(0.4)
    assert result["passed"] is False
    assert result["completion"] == pytest.approx(0.4)


def test_build_trial_entry_carries_trace_file_and_usage(tmp_path: Path) -> None:
    entry = _trial_entry(tmp_path)

    assert entry["trial"] == 1
    assert entry["trace_file"].endswith(TRACE_NAME)
    assert entry["input_tokens"] == 34567
    assert entry["output_tokens"] == 1234
    assert entry["model_time_s"] == 380.5
    assert entry["tool_time_s"] == 50.7
    assert entry["other_time_s"] == 0.0


# ── Consumer side: summarize_results.build_table ───────────────────────────


def test_summarize_table_populates_trial_id_tokens_and_failure(tmp_path: Path) -> None:
    import summarize_results

    entry = _trial_entry(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_reports(reports_dir)
    reports = summarize_results.load_reports(str(reports_dir))

    rows = summarize_results.build_table(_batch_data(entry), reports)
    header, trial_row = rows[0], rows[1]

    trial_id = trial_row[header.index("Trial ID")]
    failure = trial_row[header.index("Failure Reason")]
    assert trial_id == TRIAL_HASH, "Trial ID cell must come from trace_file"
    assert trial_row[header.index("Input Toks")] == "34567"
    assert trial_row[header.index("Output Toks")] == "1234"
    assert trial_row[header.index("Model Time(s)")] == "380.50"
    assert trial_row[header.index("Tool Time(s)")] == "50.70"
    assert trial_row[header.index("Other Time(s)")] == "0"
    assert trial_row[header.index("Wall Time(s)")] == fmt_wall(entry)
    assert failure == "tool_loop | agent 在同一工具上循环", (
        "Failure Reason must join the report keyed by trace_file basename")


def fmt_wall(entry: dict) -> str:
    import summarize_results
    return summarize_results.fmt(entry["wall_time_s"])


# ── Consumer side: analyze.build_summary_table ─────────────────────────────


def test_analyze_table_populates_trial_id_tokens_and_failure(tmp_path: Path) -> None:
    import analyze

    entry = _trial_entry(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_reports(reports_dir)
    reports = analyze.load_reports(str(reports_dir))

    rows = analyze.build_summary_table(_batch_data(entry), reports)
    header, trial_row = rows[0], rows[1]

    trial_id = trial_row[header.index("Trial ID")]
    failure = trial_row[header.index("Failure Reason")]
    assert trial_id == TRIAL_HASH, "Trial ID cell must come from trace_file"
    assert trial_row[header.index("Input Toks")] == "34567"
    assert trial_row[header.index("Output Toks")] == "1234"
    assert trial_row[header.index("Model Time(s)")] == "380.50"
    assert trial_row[header.index("Tool Time(s)")] == "50.70"
    assert failure == "tool_loop | agent 在同一工具上循环"


def test_analyze_load_reports_keys_by_trace_file(tmp_path: Path) -> None:
    import analyze

    reports_dir = tmp_path / "reports"
    _write_reports(reports_dir)
    reports = analyze.load_reports(str(reports_dir))

    assert TRACE_NAME in reports, "reports must be keyed by trace filename"
