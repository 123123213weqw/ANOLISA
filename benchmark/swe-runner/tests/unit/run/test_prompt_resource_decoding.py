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

"""Decoding-failure behavior of the optional/required prompt resource loaders.

Non-UTF-8 prompt and skill resources must follow the existing optional-resource
fallback (warn and continue without the resource) instead of aborting prompt
construction, and required loaders must keep their path-attributed RuntimeError
contract with the decoding exception retained as the cause.
"""

from pathlib import Path

import pytest

from swe_runner.agents.openclaw.prompts import build_openclaw_prompt
from swe_runner.common.models import SWEInstance
from swe_runner.run.prompting.prompt_resources import (
    load_builtin_skill_text,
    load_custom_prompt,
    load_optional_builtin_skill_text,
    load_required_custom_prompt,
)
from swe_runner.run.prompting.prompts import build_prompt

INVALID_BYTE = b"guidance \xff\xfe invalid"
TRUNCATED_UTF8 = b"guidance \xe4\xb8"


def _make_instance() -> SWEInstance:
    return SWEInstance(
        instance_id="django__django-12345",
        repo="django/django",
        version="3.2",
        base_commit="abc123",
        problem_statement="Fix the bug in the ORM layer",
        patch="",
        test_patch="",
    )


def _write_prompt(dir_path: Path, instance_id: str, payload: bytes) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    target = dir_path / instance_id
    target.write_bytes(payload)
    return target


def _write_skill(dir_path: Path, payload: bytes) -> Path:
    skill_dir = dir_path / "swe-bench-patch-generation"
    skill_dir.mkdir(parents=True, exist_ok=True)
    target = skill_dir / "SKILL.md"
    target.write_bytes(payload)
    return target


class TestOptionalCustomPromptFallback:
    def test_invalid_byte_warns_and_falls_back(self, tmp_path, caplog):
        _write_prompt(tmp_path, "case-1", INVALID_BYTE)

        with caplog.at_level("WARNING"):
            result = load_custom_prompt("case-1", prompts_dir=tmp_path)

        assert result is None
        assert "CUSTOM_PROMPT_LOAD_FAILED" in caplog.text

    def test_truncated_utf8_warns_and_falls_back(self, tmp_path, caplog):
        _write_prompt(tmp_path, "case-2", TRUNCATED_UTF8)

        with caplog.at_level("WARNING"):
            result = load_custom_prompt("case-2", prompts_dir=tmp_path)

        assert result is None
        assert "CUSTOM_PROMPT_LOAD_FAILED" in caplog.text


class TestRequiredLoadersKeepContract:
    def test_required_custom_prompt_invalid_byte_raises_runtime_error(self, tmp_path):
        target = _write_prompt(tmp_path, "case-3", INVALID_BYTE)

        with pytest.raises(RuntimeError) as excinfo:
            load_required_custom_prompt("case-3", prompts_dir=tmp_path)

        assert str(target) in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, UnicodeDecodeError)

    def test_required_custom_prompt_truncated_utf8_raises_runtime_error(self, tmp_path):
        target = _write_prompt(tmp_path, "case-4", TRUNCATED_UTF8)

        with pytest.raises(RuntimeError) as excinfo:
            load_required_custom_prompt("case-4", prompts_dir=tmp_path)

        assert str(target) in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, UnicodeDecodeError)

    def test_required_skill_invalid_byte_raises_runtime_error(self, tmp_path):
        target = _write_skill(tmp_path, INVALID_BYTE)

        with pytest.raises(RuntimeError) as excinfo:
            load_builtin_skill_text(skills_dir=tmp_path)

        assert str(target) in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, UnicodeDecodeError)


class TestOptionalSkillFallback:
    def test_invalid_byte_warns_and_falls_back(self, tmp_path, caplog):
        _write_skill(tmp_path, INVALID_BYTE)

        with caplog.at_level("WARNING"):
            result = load_optional_builtin_skill_text(skills_dir=tmp_path)

        assert result is None
        assert "BUILTIN_SKILL_UNAVAILABLE" in caplog.text


class TestPromptConstructionFallback:
    def test_cosh_prompt_builds_without_undecodable_custom_prompt(self, tmp_path, caplog):
        _write_prompt(tmp_path, "django__django-12345", INVALID_BYTE)

        with caplog.at_level("WARNING"):
            prompt = build_prompt(
                instance=_make_instance(),
                work_dir=Path("/testbed"),
                container_name="test-container",
                use_per_case_prompt=True,
                prompts_dir=tmp_path,
            )

        assert "custom_instructions" not in prompt
        assert "Fix the bug in the ORM layer" in prompt
        assert "CUSTOM_PROMPT_LOAD_FAILED" in caplog.text

    def test_openclaw_prompt_builds_without_undecodable_custom_prompt(self, tmp_path, caplog):
        _write_prompt(tmp_path, "django__django-12345", INVALID_BYTE)

        with caplog.at_level("WARNING"):
            prompt = build_openclaw_prompt(
                _make_instance(),
                use_per_case_prompt=True,
                prompts_dir=tmp_path,
            )

        assert "task_guidance" not in prompt
        assert "Fix the bug in the ORM layer" in prompt
        assert "PER_CASE_PROMPT_SKIPPED" in caplog.text


class TestValidMultilingualControl:
    def test_valid_multilingual_custom_prompt_loads(self, tmp_path):
        text = "补丁生成指引 — émoji 🐛 と日本語"
        _write_prompt(tmp_path, "case-5", text.encode("utf-8"))

        result = load_custom_prompt("case-5", prompts_dir=tmp_path)

        assert result == text
