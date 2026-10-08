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

"""The converter must order trace events by the instant a timestamp denotes,
not by its raw text: session files carry local-offset timestamps while
synthesized ones are UTC."""

import json

from ce_runner.session_trace_converter import convert_session_to_trace

TASK = {"task_id": "T900_order"}


def _message(timestamp: str, role: str, text: str) -> str:
    return json.dumps(
        {
            "type": "message",
            "timestamp": timestamp,
            "message": {
                "role": role,
                "content": [{"type": "text", "text": text}],
            },
        }
    )


def _convert(tmp_path, lines) -> list[dict]:
    session = tmp_path / "session.jsonl"
    session.write_text("\n".join(lines) + "\n", encoding="utf-8")
    output = tmp_path / "out.jsonl"
    meta = convert_session_to_trace(
        str(session), TASK, str(output), preloaded_audit_data={}
    )
    assert meta["trace_id"]
    events = [
        json.loads(line)
        for line in output.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [e for e in events if e.get("type") == "message"]


def test_mixed_offset_events_order_by_instant(tmp_path):
    # 11:00+08:00 is 03:00Z — strictly earlier than 09:30+00:00 — but the
    # raw string "…11:00…" sorts after "…09:30…", flipping the dialogue.
    messages = _convert(
        tmp_path,
        [
            _message("2026-05-27T11:00:00+08:00", "user", "the question"),
            _message("2026-05-27T09:30:00+00:00", "assistant", "the answer"),
        ],
    )
    roles = [m["message"]["role"] for m in messages]
    texts = [
        block.get("text") or block.get("content")
        for m in messages
        for block in m["message"]["content"]
    ]
    assert roles == ["user", "assistant"], f"order flipped: {roles}"
    assert "the question" in json.dumps(texts, ensure_ascii=False)


def test_same_offset_events_keep_their_order(tmp_path):
    messages = _convert(
        tmp_path,
        [
            _message("2026-05-27T01:00:00+00:00", "user", "first"),
            _message("2026-05-27T01:05:00+00:00", "assistant", "second"),
        ],
    )
    roles = [m["message"]["role"] for m in messages]
    assert roles == ["user", "assistant"]


def test_naive_timestamps_are_treated_as_utc(tmp_path):
    # A naive stamp denoting 02:00Z must sort before 09:30Z even though the
    # text "02:00" already does — pin that naive input is interpreted, not
    # scattered, by mixing it with an explicit-offset later instant.
    messages = _convert(
        tmp_path,
        [
            _message("2026-05-27T17:00:00+08:00", "user", "q"),  # 09:00Z
            _message("2026-05-27T01:30:00", "assistant", "a"),  # naive 01:30Z
        ],
    )
    roles = [m["message"]["role"] for m in messages]
    assert roles == ["assistant", "user"], (
        "the naive 01:30Z event is the earlier instant and must sort first, "
        f"got {roles}"
    )
