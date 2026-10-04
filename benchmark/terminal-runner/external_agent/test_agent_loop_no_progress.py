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

"""The unlimited agent loop must abort when OpenClaw never sends commands.

``OPENCLAW_MAX_ITERATIONS=0`` means *unlimited*, and the re-prompt loop
answers a command-less OpenClaw response by asking again — forever.  A
stub agent that never emits ```bash``` blocks (and never says
TASK_COMPLETE) therefore spun the loop with no progress guard, burning
one OpenClaw subprocess per iteration until the operator killed the run.

These tests drive ``_run_agent_loop`` with a stubbed OpenClaw subprocess
and assert the loop terminates with a distinct no-progress error, while
productive loops and explicit finite iteration caps keep their existing
behavior.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest
from openclaw_external_agent import OpenClawExternalAgent

# Hard ceiling for stub invocations.  The no-progress guard must abort far
# below it; if the stub ever raises, the loop did not terminate on its own.
STUB_CALL_CEILING = 500


class _StubOpenClaw:
    """Replace ``_sync_run_openclaw``; record calls, script the replies."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0

    def __call__(
        self, cmd: list[str], extra_env: dict[str, str] | None = None
    ) -> tuple[int, str, str]:
        self.calls += 1
        if self.calls > STUB_CALL_CEILING:
            raise AssertionError(
                f"agent loop exceeded {STUB_CALL_CEILING} stub calls without exiting"
            )
        reply = (
            self.replies[self.calls - 1]
            if self.calls <= len(self.replies)
            else self.replies[-1]
        )
        return 0, "", reply


def _reply_no_commands(reasoning: str = "Analyzing the task first.") -> str:
    """An OpenClaw JSON reply without bash commands and without TASK_COMPLETE."""
    return json.dumps({"meta": {"finalAssistantVisibleText": reasoning}})


def _reply_with_command(command: str) -> str:
    return json.dumps(
        {
            "meta": {"finalAssistantVisibleText": f"Step:\n```bash\n{command}\n```"},
        }
    )


def _reply_task_complete() -> str:
    return json.dumps({"meta": {"finalAssistantVisibleText": "TASK_COMPLETE"}})


def _make_agent(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> OpenClawExternalAgent:
    """Build an agent whose profile/workspace writes stay under *tmp_path*."""
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


async def _run_loop(agent: OpenClawExternalAgent) -> dict[str, Any]:
    return await asyncio.wait_for(
        agent._run_agent_loop("do the task", _FakeEnvironment()), timeout=60
    )


class _FakeEnvironment:
    session_id = "sess-deadbeef"

    async def exec(self, command: str) -> Any:
        class _ExecResult:
            exit_code = 0
            returncode = 0
            stdout = f"ran: {command}"
            stderr = ""

        return _ExecResult()


@pytest.mark.asyncio
async def test_unlimited_loop_aborts_on_persistent_no_progress(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """max_iterations=0 + a never-commanding agent must not spin forever."""
    monkeypatch.setenv("OPENCLAW_MAX_ITERATIONS", "0")
    agent = _make_agent(tmp_path, monkeypatch)
    stub = _StubOpenClaw([_reply_no_commands()])
    monkeypatch.setattr(OpenClawExternalAgent, "_sync_run_openclaw", stub)

    result = await _run_loop(agent)

    assert stub.calls <= STUB_CALL_CEILING, "loop never terminated"
    assert stub.calls < 100, f"loop ran away: {stub.calls} iterations"
    assert result["returncode"] != 0
    assert "no commands" in result["stderr"].lower(), result["stderr"]


@pytest.mark.asyncio
async def test_unbounded_task_complete_loop_also_aborts(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TASK_COMPLETE without any executed command re-prompts forever too."""
    monkeypatch.setenv("OPENCLAW_MAX_ITERATIONS", "0")
    agent = _make_agent(tmp_path, monkeypatch)
    stub = _StubOpenClaw([_reply_task_complete()])
    monkeypatch.setattr(OpenClawExternalAgent, "_sync_run_openclaw", stub)

    result = await _run_loop(agent)

    assert stub.calls < 100, f"loop ran away: {stub.calls} iterations"
    assert result["returncode"] != 0
    assert "no commands" in result["stderr"].lower(), result["stderr"]


@pytest.mark.asyncio
async def test_productive_loop_with_recovery_is_unaffected(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that finds commands after a few empty replies still succeeds."""
    monkeypatch.setenv("OPENCLAW_MAX_ITERATIONS", "0")
    agent = _make_agent(tmp_path, monkeypatch)
    replies = [
        _reply_no_commands(),
        _reply_no_commands(),
        _reply_with_command("echo hello"),
        _reply_task_complete(),
    ]
    stub = _StubOpenClaw(replies)
    monkeypatch.setattr(OpenClawExternalAgent, "_sync_run_openclaw", stub)

    result = await _run_loop(agent)

    assert result["returncode"] == 0
    assert stub.calls == 4
    assert len(result["harbor_executions"]) == 1
    assert result["harbor_executions"][0]["command"] == "echo hello"


@pytest.mark.asyncio
async def test_explicit_finite_max_iterations_still_bounds_the_loop(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finite cap keeps terminating the loop with the classic error."""
    monkeypatch.setenv("OPENCLAW_MAX_ITERATIONS", "3")
    agent = _make_agent(tmp_path, monkeypatch)
    stub = _StubOpenClaw([_reply_no_commands()])
    monkeypatch.setattr(OpenClawExternalAgent, "_sync_run_openclaw", stub)

    result = await _run_loop(agent)

    assert stub.calls == 3
    assert result["returncode"] != 0
    assert "Max iterations reached" in result["stderr"]
