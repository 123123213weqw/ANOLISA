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

"""Tests for scripts/check_api_key.py."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_api_key.py"


@pytest.fixture(scope="module")
def script_mod():
    spec = importlib.util.spec_from_file_location("test_api_key_script", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["test_api_key_script"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_collect_targets_reads_three_roles(script_mod, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "model:\n"
        "  api_key: k1\n"
        "  base_url: https://a/v1\n"
        "  model_id: m1\n"
        "judge:\n"
        "  api_key: k2\n"
        "  base_url: https://b/v1\n"
        "  model_id: m2\n"
        "user_agent_model:\n"
        "  api_key: k1\n"
        "  base_url: https://a/v1\n"
        "  model_id: m1\n"
    )
    targets = script_mod.collect_targets_from_config(cfg)
    assert [t["role"] for t in targets] == ["model", "judge", "user_agent_model"]


def test_collect_targets_skips_incomplete_role(script_mod, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "model:\n"
        "  api_key: k1\n"
        "  base_url: https://a/v1\n"
        "  model_id: m1\n"
        "judge:\n"
        "  api_key: k2\n"
    )
    targets = script_mod.collect_targets_from_config(cfg)
    assert [t["role"] for t in targets] == ["model"]


def test_dedupe_merges_roles(script_mod):
    raw = [
        {"role": "model", "api_key": "k", "base_url": "u", "model_id": "m"},
        {"role": "judge", "api_key": "k", "base_url": "u", "model_id": "m"},
        {"role": "user_agent_model", "api_key": "k2", "base_url": "u", "model_id": "m"},
    ]
    deduped = script_mod.dedupe_targets(raw)
    assert len(deduped) == 2
    assert deduped[0]["role"] == "model+judge"
    assert deduped[1]["role"] == "user_agent_model"


def test_mask_short_and_long(script_mod):
    assert script_mod._mask("") == "<empty>"
    assert script_mod._mask("short") == "***"
    assert script_mod._mask("sk-1234567890abcd") == "sk-1...abcd"


def test_main_cli_requires_base_url_and_model_id(script_mod, capsys):
    rc = script_mod.main(["--api-key", "sk-x"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--base-url" in err and "--model-id" in err


def test_main_config_missing_returns_2(script_mod, tmp_path, capsys):
    rc = script_mod.main(["--config", str(tmp_path / "missing.yaml")])
    assert rc == 2
    assert "config not found" in capsys.readouterr().err


def test_main_runs_with_cli_target_success(script_mod, capsys):
    fake_result = {"ok": True, "error": "", "latency_ms": 12, "reply": "pong"}
    with patch.object(script_mod, "test_one", return_value=fake_result) as m:
        rc = script_mod.main([
            "--api-key", "sk-x",
            "--base-url", "https://a/v1",
            "--model-id", "m1",
        ])
    assert rc == 0
    assert m.call_count == 1
    out = capsys.readouterr().out
    assert "1/1 passed" in out


def test_main_dedupes_yaml_roles_by_default(script_mod, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "model:\n  api_key: k\n  base_url: u\n  model_id: m\n"
        "judge:\n  api_key: k\n  base_url: u\n  model_id: m\n"
        "user_agent_model:\n  api_key: k\n  base_url: u\n  model_id: m\n"
    )
    fake_result = {"ok": True, "error": "", "latency_ms": 1, "reply": "x"}
    with patch.object(script_mod, "test_one", return_value=fake_result) as m:
        rc = script_mod.main(["--config", str(cfg)])
    assert rc == 0
    assert m.call_count == 1


def test_main_no_dedupe_runs_each_role(script_mod, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "model:\n  api_key: k\n  base_url: u\n  model_id: m\n"
        "judge:\n  api_key: k\n  base_url: u\n  model_id: m\n"
        "user_agent_model:\n  api_key: k\n  base_url: u\n  model_id: m\n"
    )
    fake_result = {"ok": True, "error": "", "latency_ms": 1, "reply": "x"}
    with patch.object(script_mod, "test_one", return_value=fake_result) as m:
        rc = script_mod.main(["--config", str(cfg), "--no-dedupe"])
    assert rc == 0
    assert m.call_count == 3


def test_main_returns_1_on_failure(script_mod):
    fake_result = {"ok": False, "error": "AuthError: bad key", "latency_ms": 5, "reply": ""}
    with patch.object(script_mod, "test_one", return_value=fake_result):
        rc = script_mod.main([
            "--api-key", "sk-x",
            "--base-url", "https://a/v1",
            "--model-id", "m1",
        ])
    assert rc == 1


BAD_BASE_URL = "https://api.invalid:notaport"


def _fake_openai_factory(monkeypatch):
    """Patch openai.OpenAI: real constructor for the malformed URL, stub otherwise."""
    from unittest.mock import MagicMock

    import openai

    real_openai = openai.OpenAI

    def _make_client(**kwargs):
        if kwargs.get("base_url") == BAD_BASE_URL:
            return real_openai(**kwargs)
        client = MagicMock()
        message = MagicMock()
        message.content = "pong"
        choice = MagicMock()
        choice.message = message
        resp = MagicMock()
        resp.choices = [choice]
        client.chat.completions.create.return_value = resp
        return client

    monkeypatch.setattr(openai, "OpenAI", _make_client)
    return _make_client


def test_test_one_reports_constructor_failure_as_result(script_mod, monkeypatch):
    _fake_openai_factory(monkeypatch)
    target = {
        "role": "model",
        "api_key": "sk-x",
        "base_url": BAD_BASE_URL,
        "model_id": "m1",
    }
    result = script_mod.test_one(target, timeout=1.0, max_tokens=5)
    assert result["ok"] is False
    assert "InvalidURL" in result["error"]
    assert result["reply"] == ""


def test_main_constructor_failure_keeps_other_roles_and_summary(script_mod, monkeypatch, tmp_path, capsys):
    _fake_openai_factory(monkeypatch)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"model:\n  api_key: k-bad\n  base_url: {BAD_BASE_URL}\n  model_id: m1\n"
        "judge:\n  api_key: k2\n  base_url: https://ok-a/v1\n  model_id: m2\n"
        "user_agent_model:\n  api_key: k3\n  base_url: https://ok-b/v1\n  model_id: m3\n"
    )
    rc = script_mod.main(["--config", str(cfg), "--no-dedupe"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "[judge]" in out and "OK" in out
    assert "[user_agent_model]" in out
    assert "InvalidURL" in out
    assert "2/3 passed" in out


def test_test_one_success_path_with_stubbed_client(script_mod, monkeypatch):
    _fake_openai_factory(monkeypatch)
    target = {
        "role": "model",
        "api_key": "sk-x",
        "base_url": "https://ok-a/v1",
        "model_id": "m1",
    }
    result = script_mod.test_one(target, timeout=1.0, max_tokens=5)
    assert result["ok"] is True
    assert result["error"] == ""
    assert result["reply"] == "pong"


def test_test_one_propagates_keyboard_interrupt_from_constructor(script_mod, monkeypatch):
    from unittest.mock import MagicMock

    import openai

    def _raising_client(**_kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(openai, "OpenAI", _raising_client)
    target = {
        "role": "model",
        "api_key": "sk-x",
        "base_url": "https://ok-a/v1",
        "model_id": "m1",
    }
    with pytest.raises(KeyboardInterrupt):
        script_mod.test_one(target, timeout=1.0, max_tokens=5)
