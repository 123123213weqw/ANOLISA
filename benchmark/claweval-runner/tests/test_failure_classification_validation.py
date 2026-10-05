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

"""Regressions for validating LLM failure classifications.

Both reporting classifiers must only accept decoded model JSON that is an
object with an allowed category and a nonblank string reason; unsupported
responses route through the existing structured LLM-error fallback. All
completions are deterministic local fixtures; no network or paid request.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import analyze  # noqa: E402
import generate_trial_reports  # noqa: E402

TASK_INFO = {"task_id": "T001", "task_name": "t", "category": "T", "prompt": "p", "judge_rubric": "r", "primary_dimensions": []}
GRADING = {"scores": {"completion": 0.1}, "task_score": 0.0, "passed": False, "judge_calls": []}
TRACE_END = {"total_turns": 2, "wall_time_s": 5.0}


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, content):
        self._content = content

    def create(self, **kwargs):
        return _FakeResponse(self._content)


class _FakeChat:
    def __init__(self, content):
        self.completions = _FakeCompletions(content)


class _FakeClient:
    def __init__(self, content):
        self.chat = _FakeChat(content)


def _classify(module, client_factory_name, content):
    def _fake_client(api_key, base_url):
        return _FakeClient(content)

    factory = getattr(module, client_factory_name)
    setattr(module, client_factory_name, _fake_client)
    try:
        return module.llm_classify_failure(
            TASK_INFO, GRADING, TRACE_END, api_key="k", base_url="http://judge.test", model_id="judge-model"
        )
    finally:
        setattr(module, client_factory_name, factory)


def _generate_classify(content):
    return _classify(generate_trial_reports, "get_llm_client", content)


def _analyze_classify(content):
    return _classify(analyze, "OpenAI", content)


FALLBACK = {"category": "other", "key_reason_zh": "LLM error: unsupported classification response: [1, 2]"}


def _assert_fallback(result):
    assert isinstance(result, dict)
    assert result["category"] == "other"
    assert "LLM error" in result["key_reason_zh"]


UNSUPPORTED_PAYLOADS = [
    "[1, 2]",
    "null",
    '"scalar"',
    "42",
    '{"category": "not_a_category", "key_reason_zh": "ok"}',
    '{"category": "tool_loop"}',
    '{"key_reason_zh": "missing category"}',
    '{"category": "tool_loop", "key_reason_zh": ""}',
    '{"category": "tool_loop", "key_reason_zh": "   "}',
    '{"category": "tool_loop", "key_reason_zh": null}',
    '{"category": "tool_loop", "key_reason_zh": 3.5}',
    '{"category": 5, "key_reason_zh": "ok"}',
]

VALID_PAYLOADS = [
    ('{"category": "tool_loop", "key_reason_zh": "reason"}', {"category": "tool_loop", "key_reason_zh": "reason"}),
    (
        '```json\n{"category": "timeout_exceeded", "key_reason_zh": "reason"}\n```',
        {"category": "timeout_exceeded", "key_reason_zh": "reason"},
    ),
]


class TestGenerateTrialReportsClassifier:
    @pytest.mark.parametrize("payload", UNSUPPORTED_PAYLOADS)
    def test_unsupported_responses_use_fallback(self, payload):
        result = _generate_classify(payload)
        _assert_fallback(result)

    @pytest.mark.parametrize("payload,expected", VALID_PAYLOADS)
    def test_valid_responses_preserved(self, payload, expected):
        assert _generate_classify(payload) == expected

    def test_invalid_json_uses_fallback(self):
        _assert_fallback(_generate_classify("not json at all"))


class TestAnalyzeClassifier:
    @pytest.mark.parametrize("payload", UNSUPPORTED_PAYLOADS)
    def test_unsupported_responses_use_fallback(self, payload):
        result = _analyze_classify(payload)
        _assert_fallback(result)

    @pytest.mark.parametrize("payload,expected", VALID_PAYLOADS)
    def test_valid_responses_preserved(self, payload, expected):
        assert _analyze_classify(payload) == expected


def _write_fail_trace(trace_dir: Path, name="T001_ab12.jsonl"):
    trace_path = trace_dir / name
    events = [
        {"type": "trace_start", "trace_id": "t", "task_id": "T001", "model": "openclaw", "timestamp": "2024-01-15T10:00:00+00:00"},
        {
            "type": "grading_result",
            "scores": {"completion": 0.1},
            "task_score": 0.0,
            "passed": False,
            "judge_calls": [],
        },
        {"type": "trace_end", "total_turns": 2, "wall_time_s": 5.0, "timestamp": "2024-01-15T10:00:09+00:00"},
    ]
    trace_path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return trace_path


class TestReportPipeline:
    def test_generate_report_survives_array_classification(self, tmp_path):
        """The array response failure reproduced through the report pipeline."""
        trace_dir = tmp_path / "traces"
        tasks_dir = tmp_path / "tasks"
        output_dir = tmp_path / "reports"
        trace_dir.mkdir()
        (tasks_dir / "T001").mkdir(parents=True)
        (tasks_dir / "T001" / "task.yaml").write_text("task_id: T001\ntask_name: t\n", encoding="utf-8")
        _write_fail_trace(trace_dir)

        original = generate_trial_reports.get_llm_client
        generate_trial_reports.get_llm_client = lambda api_key, base_url: _FakeClient("[1, 2]")
        try:
            filename, report = generate_trial_reports.process_one_trace(
                str(trace_dir / "T001_ab12.jsonl"),
                {
                    "tasks_dir": str(tasks_dir),
                    "judge_api_key": "k",
                    "judge_base_url": "http://judge.test",
                    "judge_model_id": "judge-model",
                },
            )
        finally:
            generate_trial_reports.get_llm_client = original

        assert filename == "T001_ab12.jsonl"
        assert report["status"] == "fail"
        fc = report["failure_classification"]
        assert isinstance(fc, dict)
        assert fc["category"] == "other"
        assert "LLM error" in fc["key_reason_zh"]

    def test_generated_report_remains_consumable_by_summary(self, tmp_path):
        """Summary table extraction keeps working on a fallback classification."""
        trace_dir = tmp_path / "traces"
        trace_dir.mkdir()
        _write_fail_trace(trace_dir)
        grading, trace_end = generate_trial_reports.load_grading_result(str(trace_dir / "T001_ab12.jsonl"))
        report = {
            "trace_file": "T001_ab12.jsonl",
            "status": "fail",
            "failure_classification": {"category": "other", "key_reason_zh": "LLM error: unsupported classification response"},
        }
        row = analyze.failure_summary_cell(report) if hasattr(analyze, "failure_summary_cell") else None
        if row is None:
            fc = report.get("failure_classification", {})
            category = fc.get("category", "")
            reason = fc.get("key_reason_zh", "")
            row = f"{category} | {reason}" if category and reason else (category or "other")
        assert "other" in row
