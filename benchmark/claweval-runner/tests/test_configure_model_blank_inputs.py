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

"""Actual-CLI subprocess tests for blank-credential rejection in configure_model.py.

Invalid interactive or CLI model settings must exit nonzero with a role/field
error and leave every previous YAML byte-for-byte intact, and must not create
the default config file when nothing existed. Valid separate-role and default
configurations keep succeeding.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "configure_model.py"

CONFIG_FILES = [
    "config.yaml",
    "config_general.yaml",
    "config_multimodal.yaml",
    "config_user_agent.yaml",
]

BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
MODEL_ID = "qwen3.6-plus"


def run_script(*args: str, stdin: str = "", env_extra: dict | None = None):
    env = {k: v for k, v in os.environ.items() if k != "JUDGE_MODEL_ID"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def write_existing_configs(config_dir: Path) -> dict[str, bytes]:
    """Create the four pre-existing YAMLs with per-file markers."""
    before = {}
    for name in CONFIG_FILES:
        path = config_dir / name
        path.write_text(
            f"# marker {name}\n"
            "model:\n"
            "  api_key: old-key\n"
            "  base_url: https://old/v1\n"
            "  model_id: old-model\n"
            "judge:\n"
            "  api_key: old-judge\n"
            "  base_url: https://old/v1\n"
            "  model_id: old-judge-model\n"
            "  enabled: true\n"
        )
        before[name] = path.read_bytes()
    return before


def assert_preserved(config_dir: Path, before: dict[str, bytes]) -> None:
    for name, payload in before.items():
        assert (config_dir / name).read_bytes() == payload, f"{name} was rewritten"


def assert_rejected(result) -> None:
    assert result.returncode == 1
    assert "Error" in result.stderr


class TestCliBlankInputsPreserveYamls:
    def test_whitespace_api_key_preserves_all_yamls(self, tmp_path):
        config_dir = tmp_path / "claw-eval"
        config_dir.mkdir()
        before = write_existing_configs(config_dir)

        result = run_script("--config-dir", str(config_dir), "--api-key", "   ")

        assert_rejected(result)
        assert "model" in result.stderr and "api key" in result.stderr
        assert_preserved(config_dir, before)

    def test_whitespace_base_url_preserves_all_yamls(self, tmp_path):
        config_dir = tmp_path / "claw-eval"
        config_dir.mkdir()
        before = write_existing_configs(config_dir)

        result = run_script(
            "--config-dir", str(config_dir), "--api-key", "sk-ok",
            "--base-url", " ",
        )

        assert_rejected(result)
        assert "base url" in result.stderr
        assert_preserved(config_dir, before)

    def test_whitespace_model_id_preserves_all_yamls(self, tmp_path):
        config_dir = tmp_path / "claw-eval"
        config_dir.mkdir()
        before = write_existing_configs(config_dir)

        result = run_script(
            "--config-dir", str(config_dir), "--api-key", "sk-ok",
            "--model-id", " ",
        )

        assert_rejected(result)
        assert "model id" in result.stderr
        assert_preserved(config_dir, before)

    def test_whitespace_judge_model_id_preserves_all_yamls(self, tmp_path):
        config_dir = tmp_path / "claw-eval"
        config_dir.mkdir()
        before = write_existing_configs(config_dir)

        result = run_script(
            "--config-dir", str(config_dir), "--api-key", "sk-ok",
            "--judge-model-id", " ",
        )

        assert_rejected(result)
        assert "judge" in result.stderr and "model id" in result.stderr
        assert_preserved(config_dir, before)

    def test_invalid_cli_input_creates_no_default_config(self, tmp_path):
        config_dir = tmp_path / "claw-eval"
        config_dir.mkdir()

        result = run_script("--config-dir", str(config_dir), "--api-key", " ")

        assert_rejected(result)
        assert not (config_dir / "config.yaml").exists()
        assert list(config_dir.iterdir()) == []


def interactive_stdin(same_key: str, judge_key: str, ua_key: str) -> str:
    return "\n".join([
        "sk-main",       # API Key
        same_key,        # same key for all roles?
        judge_key,       # Judge API Key (when split)
        ua_key,          # User Agent API Key (when split)
        "",              # model Model ID (default)
        "",              # model Base URL (default)
        "",              # judge Model ID (default)
        "",              # judge Base URL (default)
        "",              # user agent Model ID (default)
        "",              # user agent Base URL (default)
    ]) + "\n"


class TestInteractiveBlankInputsPreserveYamls:
    def test_empty_judge_key_preserves_all_yamls(self, tmp_path):
        config_dir = tmp_path / "claw-eval"
        config_dir.mkdir()
        before = write_existing_configs(config_dir)

        result = run_script(
            "--config-dir", str(config_dir),
            stdin=interactive_stdin("n", "", "sk-ua"),
        )

        assert_rejected(result)
        assert "judge" in result.stderr and "api key" in result.stderr
        assert_preserved(config_dir, before)

    def test_empty_user_agent_key_preserves_all_yamls(self, tmp_path):
        config_dir = tmp_path / "claw-eval"
        config_dir.mkdir()
        before = write_existing_configs(config_dir)

        result = run_script(
            "--config-dir", str(config_dir),
            stdin=interactive_stdin("n", "sk-judge", ""),
        )

        assert_rejected(result)
        assert "user agent" in result.stderr and "api key" in result.stderr
        assert_preserved(config_dir, before)

    def test_blank_judge_env_default_rejected(self, tmp_path):
        config_dir = tmp_path / "claw-eval"
        config_dir.mkdir()
        before = write_existing_configs(config_dir)

        stdin = "\n".join([
            "sk-main",   # API Key
            "y",         # same key for all roles
            "",          # model Model ID (default)
            "",          # model Base URL (default)
            "",          # judge Model ID (blank env default, accepted enter)
            "",          # judge Base URL (default)
            "",          # user agent Model ID (default)
            "",          # user agent Base URL (default)
        ]) + "\n"
        result = run_script(
            "--config-dir", str(config_dir),
            stdin=stdin,
            env_extra={"JUDGE_MODEL_ID": ""},
        )

        assert_rejected(result)
        assert "judge" in result.stderr and "model id" in result.stderr
        assert_preserved(config_dir, before)


class TestValidRunsStillSucceed:
    def test_cli_with_judge_override_updates_files(self, tmp_path):
        config_dir = tmp_path / "claw-eval"
        config_dir.mkdir()
        write_existing_configs(config_dir)

        result = run_script(
            "--config-dir", str(config_dir),
            "--api-key", "sk-ok",
            "--judge-model-id", "judge-x",
        )

        assert result.returncode == 0, result.stderr
        main = yaml.safe_load((config_dir / "config.yaml").read_text())
        assert main["model"]["api_key"] == "sk-ok"
        assert main["model"]["model_id"] == MODEL_ID
        assert main["model"]["base_url"] == BASE_URL
        assert main["judge"]["model_id"] == "judge-x"
        assert main["user_agent_model"]["api_key"] == "sk-ok"
        general = yaml.safe_load((config_dir / "config_general.yaml").read_text())
        assert general["judge"]["enabled"] is True

    def test_interactive_separate_role_keys_update_files(self, tmp_path):
        config_dir = tmp_path / "claw-eval"
        config_dir.mkdir()
        write_existing_configs(config_dir)

        result = run_script(
            "--config-dir", str(config_dir),
            stdin=judge_override_stdin(),
        )

        assert result.returncode == 0, result.stderr
        main = yaml.safe_load((config_dir / "config.yaml").read_text())
        assert main["model"]["api_key"] == "sk-main"
        assert main["judge"]["api_key"] == "sk-judge"
        assert main["judge"]["model_id"] == "judge-model"
        assert main["user_agent_model"]["api_key"] == "sk-ua"
        assert main["model"]["input_modalities"] == ["text", "image"]


def judge_override_stdin() -> str:
    lines = [
        "sk-main",       # API Key
        "n",             # split role keys
        "sk-judge",      # Judge API Key
        "sk-ua",         # User Agent API Key
        "",              # model Model ID (default)
        "",              # model Base URL (default)
        "judge-model",   # judge Model ID (override)
        "",              # judge Base URL (default)
        "",              # user agent Model ID (default)
        "",              # user agent Base URL (default)
    ]
    return "\n".join(lines) + "\n"
