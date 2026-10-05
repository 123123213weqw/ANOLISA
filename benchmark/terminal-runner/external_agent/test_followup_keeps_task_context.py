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

"""Follow-up prompts must carry the task when the session id was lost.

``_build_followup_instruction`` takes ``original_task`` but never uses
it: the follow-up prompt is *only* the command results.  That works while
``--session-id`` gives OpenClaw its history, but the session id is taken
from ``meta.agentMeta.sessionId`` of the first reply — when a reply omits
it (payloads-only output, degraded CLI), every iteration starts a *fresh*
session, and OpenClaw receives ``Results of your commands: ...`` for a
task it has never seen.  The run then cannot converge.

These tests drive the loop with a stubbed subprocess: the first reply has
commands but no session id, and the second call's ``--message`` must
still name the task.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest
from openclaw_external_agent import OpenClawExternalAgent


class _RecordingStub:
    """Replace ``_sync_run_openclaw``; script replies, capture messages."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.messages: list[str] = []

    def __call__(
        self, cmd: list[str], extra_env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        self.messages.append(cmd[cmd.index("--message") + 1])
        reply = self.replies[len(self.messages) - 1]
        return 0, "", reply


def _reply_with_command_no_session(command: str) -> str:
    """A reply with a bash command but *no* ``agentMeta.sessionId``."""
    return json.dumps(
        {
            "meta": {
                "finalAssistantVisibleText": (
                    f"First step:\n```bash\n{command}\n```"
                ),
            },
        }
    )


def _reply_task_complete() -> str:
    return json.dumps({"meta": {"finalAssistantRawText": "TASK_COMPLETE"}})


class _FakeEnvironment:
    session_id = "sess-deadbeef"

    async def exec(self, command: str) -> Any:
        class _ExecResult:
            exit_code = 0
            returncode = 0
            stdout = f"ran: {command}"
            stderr = ""

        return _ExecResult()


def _make_agent(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> OpenClawExternalAgent:
    agent = OpenClawExternalAgent(model_name="openai/gpt-4o")
    agent._task_name = "dummy-task"
    agent._profile_name = "deadbeef"
    agent._harbor_container_id = "cid123"
    agent._harbor_image = "image:latest"
    agent._harbor_workdir = "/app"

    real_expanduser = os.path.expanduser

    def _fake_expanduser(path: str) -> str:
        if path.startswith("~"):
            return str(tmp_path / path[1:].lstrip("/"))
        return real_expanduser(path)

    monkeypatch.setattr(os.path, "expanduser", _fake_expanduser)
    return agent


@pytest.mark.asyncio
async def test_followup_without_session_id_keeps_task(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No session id -> next prompt must still tell OpenClaw the task."""
    monkeypatch.setenv("OPENCLAW_MAX_ITERATIONS", "5")
    agent = _make_agent(tmp_path, monkeypatch)
    stub = _RecordingStub([
        _reply_with_command_no_session("echo step-one"),
        _reply_task_complete(),
    ])
    monkeypatch.setattr(OpenClawExternalAgent, "_sync_run_openclaw", stub)

    result = await asyncio.wait_for(
        agent._run_agent_loop(
            "recover the archive password", _FakeEnvironment(),
        ),
        timeout=30,
    )

    assert result["returncode"] == 0
    assert len(stub.messages) == 2
    followup = stub.messages[1]
    assert "recover the archive password" in followup, (
        "follow-up prompt dropped the task; a fresh session gets command "
        f"results for an unknown task: {followup!r}"
    )
    assert "echo step-one" in followup


@pytest.mark.asyncio
async def test_followup_with_session_id_also_names_task(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with a session id, the reminder must survive (bounded size)."""
    monkeypatch.setenv("OPENCLAW_MAX_ITERATIONS", "5")
    agent = _make_agent(tmp_path, monkeypatch)
    first = json.loads(_reply_with_command_no_session("echo step-one"))
    first["meta"]["agentMeta"] = {"sessionId": "oc-sess-1234"}
    stub = _RecordingStub([
        json.dumps(first),
        _reply_task_complete(),
    ])
    monkeypatch.setattr(OpenClawExternalAgent, "_sync_run_openclaw", stub)

    result = await asyncio.wait_for(
        agent._run_agent_loop(
            "recover the archive password", _FakeEnvironment(),
        ),
        timeout=30,
    )

    assert result["returncode"] == 0
    assert "recover the archive password" in stub.messages[1]
