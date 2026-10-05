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

"""Regressions for malformed session records in claw-eval trace conversion.

Valid JSON that is not an object, non-object message payloads, null or
non-list content containers, malformed text blocks and non-object usage
containers must not stop the conversion; valid neighboring evidence is
retained and OpenClaw's string message content is preserved as a normal
text block.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from ce_runner.session_trace_converter import convert_session_to_trace  # noqa: E402

TASK = {"task_id": "T001", "services": [], "tools": []}


def _run_conversion(tmp_path, lines, task=TASK):
    session_file = tmp_path / "session.jsonl"
    session_file.write_text("\n".join(json.dumps(line) if not isinstance(line, str) else line for line in lines) + "\n")
    output_file = tmp_path / "output.jsonl"
    meta = convert_session_to_trace(str(session_file), dict(task), str(output_file))
    events = [json.loads(line) for line in output_file.read_text().strip().splitlines()]
    return meta, events


def _messages(events, role):
    return [e["message"] for e in events if e.get("type") == "message" and e.get("message", {}).get("role") == role]


_VALID_USER = {
    "type": "message",
    "timestamp": "2024-01-15T10:00:01.000Z",
    "message": {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
}
_VALID_ASSISTANT = {
    "type": "message",
    "timestamp": "2024-01-15T10:00:02.000Z",
    "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "Done"}],
        "usage": {"input": 7, "output": 3},
    },
}


class TestNonObjectRecords:
    def test_null_scalar_array_records_skip_and_keep_neighbors(self, tmp_path):
        _, events = _run_conversion(tmp_path, [None, 42, "raw-string", [1, 2], _VALID_USER, _VALID_ASSISTANT])
        assert len(_messages(events, "user")) == 1
        assert len(_messages(events, "assistant")) == 1
        assert events[0]["type"] == "trace_start"

    def test_malformed_record_diagnostic_has_source_context(self, tmp_path, capsys):
        _run_conversion(tmp_path, [_VALID_USER, '"scalar-record"', _VALID_ASSISTANT])
        err = capsys.readouterr().err
        assert "[converter] WARN" in err
        assert "line=2" in err


class TestNonObjectMessagePayloads:
    def test_non_object_payload_skips_and_keeps_neighbors(self, tmp_path):
        _, events = _run_conversion(
            tmp_path,
            [
                {"type": "message", "timestamp": "2024-01-15T10:00:00.000Z", "message": None},
                _VALID_USER,
                {"type": "message", "timestamp": "2024-01-15T10:00:03.000Z", "message": "not-a-dict"},
                {"type": "message", "timestamp": "2024-01-15T10:00:04.000Z", "message": [1, 2]},
                _VALID_ASSISTANT,
            ],
        )
        assert len(_messages(events, "user")) == 1
        assert len(_messages(events, "assistant")) == 1

    def test_non_object_payload_diagnostic(self, tmp_path, capsys):
        _run_conversion(tmp_path, [{"type": "message", "message": 5}])
        assert "message" in capsys.readouterr().err


class TestContentContainers:
    def test_null_content_user_message_converts_with_empty_content(self, tmp_path):
        event = dict(_VALID_USER)
        event["message"] = {"role": "user", "content": None}
        _, events = _run_conversion(tmp_path, [event])
        assert _messages(events, "user")[0]["content"] == []

    def test_non_list_content_user_message_converts(self, tmp_path):
        event = dict(_VALID_USER)
        event["message"] = {"role": "user", "content": 42}
        _, events = _run_conversion(tmp_path, [event])
        assert _messages(events, "user")[0]["content"] == []

    def test_string_user_content_preserved_as_text_block(self, tmp_path):
        event = dict(_VALID_USER)
        event["message"] = {"role": "user", "content": "string question"}
        _, events = _run_conversion(tmp_path, [event])
        assert _messages(events, "user")[0]["content"] == [{"type": "text", "text": "string question"}]

    def test_string_assistant_content_preserved_and_becomes_final_text(self, tmp_path):
        event = dict(_VALID_ASSISTANT)
        event["message"] = {
            "role": "assistant",
            "content": "string answer",
            "usage": {"input": 4, "output": 2},
        }
        _, events = _run_conversion(tmp_path, [event])
        assert _messages(events, "assistant")[0]["content"] == [{"type": "text", "text": "string answer"}]
        trace_end = [e for e in events if e["type"] == "trace_end"][0]
        assert trace_end["final_text"] == "string answer"

    def test_string_tool_result_content_preserved(self, tmp_path):
        lines = [
            {
                "type": "message",
                "timestamp": "2024-01-15T10:00:01.000Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "toolCall", "id": "tc-1", "name": "claw-eval-weather__lookup", "arguments": {"city": "x"}}
                    ],
                    "usage": {"input": 4, "output": 2},
                },
            },
            {
                "type": "message",
                "timestamp": "2024-01-15T10:00:02.000Z",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "tc-1",
                    "toolName": "claw-eval-weather__lookup",
                    "content": "string tool output",
                },
            },
        ]
        _, events = _run_conversion(tmp_path, lines)
        user_messages = _messages(events, "user")
        tool_result_blocks = [b for m in user_messages for b in m["content"] if b.get("type") == "tool_result"]
        assert tool_result_blocks[0]["content"] == [{"type": "text", "text": "string tool output"}]
        dispatches = [e for e in events if e["type"] == "tool_dispatch"]
        assert dispatches[0]["response_body"] == "string tool output"


class TestMalformedBlocks:
    def test_non_object_blocks_dropped_and_valid_kept(self, tmp_path):
        event = dict(_VALID_USER)
        event["message"] = {
            "role": "user",
            "content": ["bare-string", 42, None, {"type": "text", "text": "keep me"}],
        }
        _, events = _run_conversion(tmp_path, [event])
        assert _messages(events, "user")[0]["content"] == [{"type": "text", "text": "keep me"}]

    def test_text_block_without_string_text_dropped(self, tmp_path):
        event = dict(_VALID_ASSISTANT)
        event["message"] = {
            "role": "assistant",
            "content": [{"type": "text"}, {"type": "text", "text": 9}, {"type": "text", "text": "good"}],
            "usage": {"input": 4, "output": 2},
        }
        _, events = _run_conversion(tmp_path, [event])
        assert _messages(events, "assistant")[0]["content"] == [{"type": "text", "text": "good"}]
        trace_end = [e for e in events if e["type"] == "trace_end"][0]
        assert trace_end["final_text"] == "good"

    def test_malformed_block_diagnostic(self, tmp_path, capsys):
        event = dict(_VALID_USER)
        event["message"] = {"role": "user", "content": [{"type": "text"}]}
        _run_conversion(tmp_path, [event])
        err = capsys.readouterr().err
        assert "[converter] WARN" in err and "text" in err


class TestUsageContainers:
    def test_non_object_usage_converts_with_zero_tokens(self, tmp_path):
        event = dict(_VALID_ASSISTANT)
        event["message"] = {"role": "assistant", "content": [{"type": "text", "text": "answer"}], "usage": "many"}
        _, events = _run_conversion(tmp_path, [event])
        assistant_events = [e for e in events if e.get("type") == "message" and e["message"]["role"] == "assistant"]
        assert assistant_events[0]["usage"] == {"input_tokens": 0, "output_tokens": 0}
        trace_end = [e for e in events if e["type"] == "trace_end"][0]
        assert trace_end["input_tokens"] == 0
        assert trace_end["output_tokens"] == 0

    def test_null_usage_converts_with_zero_tokens(self, tmp_path):
        event = dict(_VALID_ASSISTANT)
        event["message"] = {"role": "assistant", "content": [{"type": "text", "text": "answer"}], "usage": None}
        _, events = _run_conversion(tmp_path, [event])
        assistant_events = [e for e in events if e.get("type") == "message" and e["message"]["role"] == "assistant"]
        assert assistant_events[0]["usage"] == {"input_tokens": 0, "output_tokens": 0}


class TestTimestampFallback:
    def test_null_timestamp_uses_fallback(self, tmp_path):
        event = dict(_VALID_USER)
        event["timestamp"] = None
        _, events = _run_conversion(tmp_path, [event])
        user_events = [e for e in events if e.get("type") == "message" and e["message"]["role"] == "user"]
        assert user_events[0]["timestamp"]


class TestValidControl:
    def test_valid_usage_and_blocks_unchanged(self, tmp_path):
        meta, events = _run_conversion(tmp_path, [_VALID_USER, _VALID_ASSISTANT])
        assistant_events = [e for e in events if e.get("type") == "message" and e["message"]["role"] == "assistant"]
        assert assistant_events[0]["usage"] == {"input_tokens": 7, "output_tokens": 3}
        trace_end = [e for e in events if e["type"] == "trace_end"][0]
        assert trace_end["input_tokens"] == 7
        assert trace_end["output_tokens"] == 3
        assert meta["input_tokens"] == 7
        assert meta["output_tokens"] == 3
