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

"""Test [DONE] marker recognition in the simulated user agent.

The marker completes the CE dialogue only when the stripped nonempty
response is exactly ``[DONE]``. Ordinary mentions of the marker inside a
sentence, quotation, fenced example or inline reasoning are valid
dialogue and must be forwarded to the next agent round.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from ce_runner import agent as agent_module  # noqa: E402
from ce_runner.agent import _call_user_agent_llm  # noqa: E402

UA_CONFIG = {
    "api_key": "test-key",
    "base_url": "http://127.0.0.1:1",
    "model_id": "stub-model",
}

CONVERSATION = [
    {"role": "user", "text": "[user_agent] 请帮我算一下手续费"},
    {"role": "assistant", "text": "手续费是 10 元。"},
]


def _install_fake_openai(monkeypatch: pytest.MonkeyPatch, replies: list[str]) -> MagicMock:
    """Patch ``openai.OpenAI`` to return queued string replies; no network."""
    queue = list(replies)
    fake_client = MagicMock()

    def _create(**_kwargs):
        message = MagicMock()
        message.content = queue.pop(0) if queue else "[DONE]"
        choice = MagicMock()
        choice.message = message
        resp = MagicMock()
        resp.choices = [choice]
        return resp

    fake_client.chat.completions.create.side_effect = _create

    import openai

    monkeypatch.setattr(openai, "OpenAI", lambda **_kwargs: fake_client)
    return fake_client


class TestUserAgentMarkerRecognition:
    def test_exact_done_marker_stops_dialogue(self, monkeypatch):
        _install_fake_openai(monkeypatch, ["[DONE]"])
        assert _call_user_agent_llm(UA_CONFIG, "persona", CONVERSATION) is None

    def test_marker_with_surrounding_whitespace_stops_dialogue(self, monkeypatch):
        _install_fake_openai(monkeypatch, ["  [DONE] \n"])
        assert _call_user_agent_llm(UA_CONFIG, "persona", CONVERSATION) is None

    def test_sentence_containing_marker_stays_dialogue(self, monkeypatch):
        reply = "I am not [DONE] yet; please explain the fee"
        _install_fake_openai(monkeypatch, [reply])
        assert _call_user_agent_llm(UA_CONFIG, "persona", CONVERSATION) == reply

    def test_quoted_marker_example_stays_dialogue(self, monkeypatch):
        reply = '例如 "[DONE]" 表示完成，但我还想确认利率'
        _install_fake_openai(monkeypatch, [reply])
        assert _call_user_agent_llm(UA_CONFIG, "persona", CONVERSATION) == reply

    def test_fenced_marker_example_stays_dialogue(self, monkeypatch):
        reply = "结束协议示例：\n```\n[DONE]\n```\n请继续说明手续费"
        _install_fake_openai(monkeypatch, [reply])
        assert _call_user_agent_llm(UA_CONFIG, "persona", CONVERSATION) == reply

    def test_inline_reasoning_with_marker_stays_dialogue(self, monkeypatch):
        reply = "看到 [DONE] 表示完成，不过我想再确认一下利率"
        _install_fake_openai(monkeypatch, [reply])
        assert _call_user_agent_llm(UA_CONFIG, "persona", CONVERSATION) == reply

    def test_lowercase_marker_stays_dialogue(self, monkeypatch):
        _install_fake_openai(monkeypatch, ["[done]"])
        assert _call_user_agent_llm(UA_CONFIG, "persona", CONVERSATION) == "[done]"

    def test_empty_reply_returns_none(self, monkeypatch):
        _install_fake_openai(monkeypatch, [""])
        assert _call_user_agent_llm(UA_CONFIG, "persona", CONVERSATION) is None

    def test_whitespace_reply_returns_none(self, monkeypatch):
        _install_fake_openai(monkeypatch, ["   "])
        assert _call_user_agent_llm(UA_CONFIG, "persona", CONVERSATION) is None

    def test_normal_reply_returned_verbatim(self, monkeypatch):
        _install_fake_openai(monkeypatch, ["好的，明白了"])
        assert _call_user_agent_llm(UA_CONFIG, "persona", CONVERSATION) == "好的，明白了"


class TestUserAgentDialogueLoop:
    def _install_loop_stubs(self, monkeypatch, replies: list[str]) -> list[str]:
        forwarded: list[str] = []
        monkeypatch.setattr(
            agent_module, "load_task_yaml", lambda _path: {"user_agent": {"persona": "p", "max_rounds": 3}}
        )
        monkeypatch.setattr(agent_module, "run_agent", lambda *_a, **_k: "/tmp/fake-session.json")
        monkeypatch.setattr(
            agent_module, "_get_last_assistant_has_tool_calls", lambda _session_file: False
        )
        monkeypatch.setattr(
            agent_module,
            "_build_conversation_for_user_agent",
            lambda _session_file: CONVERSATION,
        )

        def _continue(_session_id, message, _timeout, agent_id=None):
            forwarded.append(message)
            return "/tmp/fake-session.json"

        monkeypatch.setattr(agent_module, "_run_agent_continue", _continue)
        _install_fake_openai(monkeypatch, replies)
        return forwarded

    def test_marker_mention_is_forwarded_to_next_round(self, monkeypatch):
        question = "I am not [DONE] yet; please explain the fee"
        forwarded = self._install_loop_stubs(monkeypatch, [question, "[DONE]"])

        agent_module.run_agent_with_user_agent("sess-1", "/tmp/task.yaml", 10, UA_CONFIG)

        assert forwarded == [f"[user_agent]\n{question}"]

    def test_exact_marker_ends_loop_without_continuation(self, monkeypatch):
        forwarded = self._install_loop_stubs(monkeypatch, ["[DONE]"])

        agent_module.run_agent_with_user_agent("sess-1", "/tmp/task.yaml", 10, UA_CONFIG)

        assert forwarded == []
