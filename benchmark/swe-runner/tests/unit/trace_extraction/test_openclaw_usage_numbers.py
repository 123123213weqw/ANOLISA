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

"""Regressions for unusable recorded usage numbers in OpenClaw JSONL traces."""

import json

from swe_runner.trace_extraction.openclaw_jsonl import (
    _normalize_usage,
    _usage_float,
    _usage_int,
    reconstruct_openclaw_jsonl_session,
)

INPUT_KEYS = ("input", "input_tokens", "prompt_tokens", "prompt")


def _write_session(tmp_path, entries):
    session_file = tmp_path / "session-usage.jsonl"
    session_file.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
    return session_file


def _assistant_entry(entry_id, usage, content="assistant answer"):
    entry = {
        "type": "message",
        "id": entry_id,
        "role": "assistant",
        "timestamp": "2026-04-24T00:00:02+00:00",
        "model": "gpt-5.2",
        "parts": [{"type": "text", "content": content}],
    }
    if usage is not None:
        entry["usage"] = usage
    return entry


class TestUsageIntSelection:
    def test_skips_nonfinite_float_and_uses_alias(self):
        assert _usage_int({"input": float("nan"), "input_tokens": 7}, *INPUT_KEYS) == 7

    def test_infinity_returns_zero(self):
        assert _usage_int({"input": float("inf")}, *INPUT_KEYS) == 0

    def test_skips_booleans(self):
        assert _usage_int({"input": True, "input_tokens": 11}, *INPUT_KEYS) == 11

    def test_skips_negative_and_uses_alias(self):
        assert _usage_int({"input": -5, "input_tokens": 9}, *INPUT_KEYS) == 9

    def test_truncates_finite_fraction(self):
        assert _usage_int({"input": 3.9}, *INPUT_KEYS) == 3


class TestUsageFloatSelection:
    def test_skips_nan(self):
        assert _usage_float({"cost": float("nan")}, "cost", "estimated_cost") is None

    def test_skips_infinity(self):
        assert _usage_float({"cost": float("inf")}, "cost", "estimated_cost") is None

    def test_skips_booleans(self):
        assert _usage_float({"cost": True}, "cost", "estimated_cost") is None

    def test_oversized_integer_returns_none(self):
        assert _usage_float({"cost": 10**400}, "cost", "estimated_cost") is None

    def test_finite_cost_control(self):
        assert _usage_float({"cost": 0.001}, "cost", "estimated_cost") == 0.001


class TestNormalizeUsage:
    def test_invalid_values_do_not_leak_into_normalized_metrics(self):
        normalized = _normalize_usage({"input": float("nan"), "output": 5, "cost": float("inf")})
        assert normalized.get("input_tokens", 0) == 0
        assert normalized["output_tokens"] == 5
        assert "cost" not in normalized

    def test_valid_usage_control(self):
        normalized = _normalize_usage({"input": 100, "output": 25, "cost": 0.5})
        assert normalized == {"input_tokens": 100, "output_tokens": 25, "cost": 0.5}


class TestReconstructRecordedUsage:
    def test_invalid_token_field_keeps_assistant_answer(self, tmp_path):
        session_file = _write_session(
            tmp_path,
            [_assistant_entry("a-1", {"input": float("nan"), "output": 25}, "usable answer")],
        )
        trace = reconstruct_openclaw_jsonl_session(session_file)
        assert trace is not None
        assert trace["total_steps"] == 1
        assert trace["total_input_tokens"] == 0
        assert trace["total_output_tokens"] == 25
        assert trace["steps"][0]["assistant_output"][0]["content"] == "usable answer"

    def test_infinity_usage_does_not_crash(self, tmp_path):
        session_file = _write_session(tmp_path, [_assistant_entry("a-1", {"input": float("inf"), "output": 4})])
        trace = reconstruct_openclaw_jsonl_session(session_file)
        assert trace["total_input_tokens"] == 0
        assert trace["total_output_tokens"] == 4

    def test_boolean_usage_not_counted(self, tmp_path):
        session_file = _write_session(tmp_path, [_assistant_entry("a-1", {"input": True, "output": 2})])
        trace = reconstruct_openclaw_jsonl_session(session_file)
        assert trace["total_input_tokens"] == 0
        assert trace["total_output_tokens"] == 2

    def test_negative_tokens_fall_back_to_alias(self, tmp_path):
        session_file = _write_session(
            tmp_path,
            [_assistant_entry("a-1", {"input": -100, "input_tokens": 7, "output": 3})],
        )
        trace = reconstruct_openclaw_jsonl_session(session_file)
        assert trace["total_input_tokens"] == 7
        assert trace["total_output_tokens"] == 3

    def test_nonfinite_cost_excluded_from_total(self, tmp_path):
        session_file = _write_session(tmp_path, [_assistant_entry("a-1", {"input": 1, "cost": float("nan")})])
        trace = reconstruct_openclaw_jsonl_session(session_file)
        assert trace["total_cost"] is None
        assert "cost" not in trace["steps"][0]

    def test_overflowing_total_cost_unavailable_with_step_costs(self, tmp_path):
        huge_cost = 1.5e308
        session_file = _write_session(
            tmp_path,
            [
                _assistant_entry("a-1", {"input": 1, "cost": huge_cost}),
                _assistant_entry("a-2", {"input": 2, "cost": huge_cost}),
            ],
        )
        trace = reconstruct_openclaw_jsonl_session(session_file)
        assert trace["total_cost"] is None
        assert trace["steps"][0]["cost"] == huge_cost
        assert trace["steps"][1]["cost"] == huge_cost

    def test_oversized_integer_cost_skipped(self, tmp_path):
        session_file = _write_session(tmp_path, [_assistant_entry("a-1", {"input": 1, "cost": 10**400})])
        trace = reconstruct_openclaw_jsonl_session(session_file)
        assert trace["total_cost"] is None
        assert "cost" not in trace["steps"][0]
