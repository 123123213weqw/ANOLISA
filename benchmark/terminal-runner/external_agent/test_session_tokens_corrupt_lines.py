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

"""One damaged session-JSONL line must not erase the whole token report.

``_collect_session_tokens`` reads the OpenClaw session JSONL to build the
per-round token-usage summary.  Container command output flows through the
conversation, so a single line with invalid UTF-8 (or a bare JSON scalar /
array line, which makes the ``d.get`` lookup raise ``AttributeError``)
aborted the scan: the outer ``except Exception`` fired and the function
returned ``None`` — discarding every round that had already been parsed.

The swe-runner equivalent of this parser (openclaw_jsonl.py, PR #5344)
treats the same damage as a per-line skip; these tests pin that behavior
for the terminal-runner copy: damaged lines are skipped, healthy rounds
before and after them survive.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import pytest
from openclaw_external_agent import OpenClawExternalAgent


def _usage_round(inp: int, out: int) -> str:
    return json.dumps(
        {
            "type": "message",
            "message": {"usage": {
                "input": inp, "output": out, "totalTokens": inp + out,
            }},
        }
    )


def _write_session(profile_dir: str, lines: list[bytes]) -> str:
    sessions_dir = os.path.join(profile_dir, "agents", "main", "sessions")
    os.makedirs(sessions_dir, exist_ok=True)
    path = os.path.join(sessions_dir, "sess-1.jsonl")
    with open(path, "wb") as f:
        f.write(b"".join(lines))
    return path


def _make_agent() -> OpenClawExternalAgent:
    return OpenClawExternalAgent(model_name="openai/gpt-4o")


def test_invalid_utf8_line_skips_line_not_whole_report(
    tmp_path: Any,
) -> None:
    """A lone undecodable line loses itself, not the entire summary."""
    agent = _make_agent()
    profile_dir = str(tmp_path)
    _write_session(profile_dir, [
        (_usage_round(100, 20) + "\n").encode("utf-8"),
        b'{"type":"message","note":"\xff\xfe broken bytes"}\n',
        (_usage_round(300, 40) + "\n").encode("utf-8"),
    ])

    report = agent._collect_session_tokens(profile_dir, "main")

    assert report is not None, "entire report lost over one damaged line"
    assert report["num_rounds"] == 2
    assert report["total_input"] == 400
    assert report["total_output"] == 60
    assert [r["input"] for r in report["rounds"]] == [100, 300]


def test_non_object_json_line_skips_line_not_whole_report(
    tmp_path: Any,
) -> None:
    """A JSON scalar/array line must not raise past the per-line guard."""
    agent = _make_agent()
    profile_dir = str(tmp_path)
    _write_session(profile_dir, [
        (_usage_round(100, 20) + "\n").encode("utf-8"),
        b"[1, 2, 3]\n",
        b'"a bare json string"\n',
        (_usage_round(200, 30) + "\n").encode("utf-8"),
    ])

    report = agent._collect_session_tokens(profile_dir, "main")

    assert report is not None, "entire report lost over non-object lines"
    assert report["num_rounds"] == 2
    assert report["total_input"] == 300


def test_empty_and_blank_lines_still_ignored(
    tmp_path: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    """Blank lines keep being skipped without any report loss."""
    agent = _make_agent()
    profile_dir = str(tmp_path)
    _write_session(profile_dir, [
        b"\n",
        b"   \n",
        (_usage_round(50, 10) + "\n").encode("utf-8"),
    ])

    with caplog.at_level(logging.WARNING):
        report = agent._collect_session_tokens(profile_dir, "main")

    assert report is not None
    assert report["num_rounds"] == 1
    assert not [r for r in caplog.records if "failed to read" in r.getMessage()]
