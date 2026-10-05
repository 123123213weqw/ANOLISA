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

"""Completion-signal regression tests for the OpenClaw routing loop.

These tests execute the REAL asynchronous routing loop
(``OpenClawExternalAgent._run_agent_loop``) against deterministic scripted
OpenClaw replies and a harmless environment stub. No Docker, OpenClaw or
model process is launched: ``_sync_run_openclaw`` is replaced by a script
provider, ``environment.exec`` records commands and returns a fixed result,
and ``HOME`` points at a temporary directory so the loop's profile setup
touches only sandbox paths.

They pin the completion contract: the loop may only finish successfully
after at least one command execution AND when ``TASK_COMPLETE`` appears as
a standalone case-insensitive line outside code fences. Ordinary prose
mentions, quoted protocol examples and fenced code containing the token
must NOT terminate the task (the bug this suite guards against).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "external_agent" / "openclaw_external_agent.py"
)


def _install_harbor_stubs() -> None:
    """Provide minimal ``harbor`` modules so the agent module imports hermetically."""
    if "harbor" in sys.modules:
        return

    harbor = types.ModuleType("harbor")
    agents_pkg = types.ModuleType("harbor.agents")
    agents_base = types.ModuleType("harbor.agents.base")

    class BaseAgent:  # minimal stand-in for harbor.agents.base.BaseAgent
        def __init__(self, *args, **kwargs):
            self.logger = logging.getLogger("openclaw-external-agent-test")

    agents_base.BaseAgent = BaseAgent

    envs_pkg = types.ModuleType("harbor.environments")
    envs_base = types.ModuleType("harbor.environments.base")
    envs_base.BaseEnvironment = object

    models_pkg = types.ModuleType("harbor.models")
    models_agent_pkg = types.ModuleType("harbor.models.agent")
    models_ctx = types.ModuleType("harbor.models.agent.context")
    models_ctx.AgentContext = object

    harbor.agents = agents_pkg
    agents_pkg.base = agents_base
    harbor.environments = envs_pkg
    envs_pkg.base = envs_base
    harbor.models = models_pkg
    models_pkg.agent = models_agent_pkg
    models_agent_pkg.context = models_ctx

    for name, module in {
        "harbor": harbor,
        "harbor.agents": agents_pkg,
        "harbor.agents.base": agents_base,
        "harbor.environments": envs_pkg,
        "harbor.environments.base": envs_base,
        "harbor.models": models_pkg,
        "harbor.models.agent": models_agent_pkg,
        "harbor.models.agent.context": models_ctx,
    }.items():
        sys.modules[name] = module


def _load_agent_module():
    _install_harbor_stubs()
    spec = importlib.util.spec_from_file_location(
        "openclaw_external_agent_under_test", _MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _ExecResult:
    def __init__(self, exit_code=0, stdout="ok", stderr=""):
        self.exit_code = exit_code
        self.returncode = exit_code
        self.stdout = stdout
        self.stderr = stderr


class StubEnvironment:
    """Harmless harbor environment: records commands, executes nothing."""

    def __init__(self):
        self.executed: list[str] = []

    async def exec(self, command: str):
        self.executed.append(command)
        return _ExecResult(stdout=f"ran: {command}")


class CompletionSignalLoopTest(unittest.TestCase):
    """Drive the real routing loop with scripted OpenClaw replies."""

    def setUp(self):
        logging.getLogger("openclaw-external-agent-test").setLevel(logging.CRITICAL)
        self.module = _load_agent_module()
        self.agent = self.module.OpenClawExternalAgent()
        self.agent._profile_name = "unit-test-profile"
        self.agent._harbor_container_id = "test-container"
        self.agent._task_name = "unit-test"
        self.agent._skill_hint = ""
        self.agent.model_name = ""
        self.agent._extra_env = {}

        self._orig_run_openclaw = self.module.OpenClawExternalAgent._sync_run_openclaw
        self._orig_max_iter = os.environ.get("OPENCLAW_MAX_ITERATIONS")
        os.environ["OPENCLAW_MAX_ITERATIONS"] = "10"
        self._orig_home = os.environ.get("HOME")
        self._home = tempfile.TemporaryDirectory(prefix="openclaw-loop-test-")
        self.addCleanup(self._home.cleanup)
        os.environ["HOME"] = self._home.name
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        self.module.OpenClawExternalAgent._sync_run_openclaw = self._orig_run_openclaw
        if self._orig_max_iter is None:
            os.environ.pop("OPENCLAW_MAX_ITERATIONS", None)
        else:
            os.environ["OPENCLAW_MAX_ITERATIONS"] = self._orig_max_iter
        if self._orig_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._orig_home

    def _script_replies(self, replies: list[str]):
        """Replace the OpenClaw subprocess call with a deterministic script."""
        calls: list[list[str]] = []

        def fake_run_openclaw(cmd, extra_env=None):
            calls.append(list(cmd))
            index = len(calls) - 1
            text = replies[index] if index < len(replies) else "TASK_COMPLETE"
            return 0, "", json.dumps({"meta": {"finalAssistantRawText": text}})

        self.module.OpenClawExternalAgent._sync_run_openclaw = staticmethod(
            fake_run_openclaw
        )
        return calls

    def _run_loop(self, environment):
        return asyncio.run(
            self.agent._run_agent_loop("solve the unit test task", environment)
        )

    # ── Red cases: non-signals must not complete the task ────────────────────

    def test_quoted_protocol_mention_does_not_complete(self):
        env = StubEnvironment()
        calls = self._script_replies([
            "```bash\necho start\n```",
            'The protocol docs say "TASK_COMPLETE" ends the task, but I am '
            "still verifying the results.",
            "TASK_COMPLETE",
        ])
        result = self._run_loop(env)
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(
            len(calls), 3,
            "the quoted mention must re-prompt instead of completing",
        )
        self.assertEqual(env.executed, ["echo start"])

    def test_fenced_code_mention_does_not_complete(self):
        env = StubEnvironment()
        calls = self._script_replies([
            "```bash\necho one\n```",
            "```python\nprint('TASK_COMPLETE')\n```",
            "TASK_COMPLETE",
        ])
        result = self._run_loop(env)
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(
            len(calls), 3,
            "a fenced code mention must re-prompt instead of completing",
        )
        self.assertEqual(env.executed, ["echo one"])

    def test_ordinary_prose_mention_does_not_complete(self):
        env = StubEnvironment()
        calls = self._script_replies([
            "```bash\necho two\n```",
            "I am not TASK_COMPLETE yet — still checking the logs.",
            "All checks passed.\ntask_complete",
        ])
        result = self._run_loop(env)
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(
            len(calls), 3,
            "the prose mention must re-prompt instead of completing",
        )
        self.assertEqual(env.executed, ["echo two"])
        self.assertEqual(result["iterations"], 3)

    # ── Compatibility cases: real signals still complete ─────────────────────

    def test_standalone_line_completes_after_execution(self):
        env = StubEnvironment()
        calls = self._script_replies([
            "```bash\necho done-setup\n```",
            "Summary: everything verified.\nTASK_COMPLETE",
        ])
        result = self._run_loop(env)
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(env.executed, ["echo done-setup"])
        self.assertEqual(
            [e["command"] for e in result["harbor_executions"]],
            ["echo done-setup"],
        )

    def test_completion_without_execution_still_reprompts(self):
        env = StubEnvironment()
        calls = self._script_replies([
            "TASK_COMPLETE",
            "```bash\necho now-exec\n```",
            "TASK_COMPLETE",
        ])
        result = self._run_loop(env)
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(
            len(calls), 3,
            "the no-execution guard must re-prompt the leading signal",
        )
        self.assertEqual(env.executed, ["echo now-exec"])

    # ── Unit checks for the signal recognizer itself ─────────────────────────

    def test_signal_recognizer_contract(self):
        has = self.module.OpenClawExternalAgent._has_completion_signal
        self.assertTrue(has("TASK_COMPLETE"))
        self.assertTrue(has("task_complete"))
        self.assertTrue(has("Task_Complete"))
        self.assertTrue(has("Working...\n  TASK_COMPLETE  \n"))
        self.assertFalse(has(""))
        self.assertFalse(has("I am not TASK_COMPLETE yet"))
        self.assertFalse(has('say "TASK_COMPLETE" to finish'))
        self.assertFalse(has("```bash\necho TASK_COMPLETE\n```"))
        self.assertFalse(has("```\nTASK_COMPLETE\n```"))
        self.assertFalse(has("Inline `TASK_COMPLETE` in prose"))
        self.assertFalse(has("TASK_COMPLETE."))


if __name__ == "__main__":
    unittest.main()
