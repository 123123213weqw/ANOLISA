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

"""Tests for CLI propagation of dataset shard options."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from swe_runner.cli import app
from swe_runner.cli_commands import build_run_settings
from swe_runner.run.io.report import RunReport

runner = CliRunner()


def test_build_run_settings_propagates_shard_options(tmp_path: Path) -> None:
    settings = build_run_settings(
        agent="openclaw",
        subset="lite",
        split="test",
        output=tmp_path,
        timeout=120,
        step_limit=0,
        slice_range=None,
        filter_regex=None,
        instance_id=None,
        workers=1,
        docker_pull_registry=None,
        use_skill=False,
        skills_dir=None,
        tokenless=False,
        per_case_prompt=False,
        prompts_dir=None,
        num_shards=4,
        shard_index=2,
    )

    assert settings.dataset.num_shards == 4
    assert settings.dataset.shard_index == 2


def test_build_run_settings_defaults_to_single_shard(tmp_path: Path) -> None:
    settings = build_run_settings(
        agent="openclaw",
        subset="lite",
        split="test",
        output=tmp_path,
        timeout=120,
        step_limit=0,
        slice_range=None,
        filter_regex=None,
        instance_id=None,
        workers=1,
        docker_pull_registry=None,
        use_skill=False,
        skills_dir=None,
        tokenless=False,
        per_case_prompt=False,
        prompts_dir=None,
    )

    assert settings.dataset.num_shards == 1
    assert settings.dataset.shard_index == 0


def test_build_run_settings_rejects_invalid_shard_range_before_session(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        build_run_settings(
            agent="openclaw",
            subset="lite",
            split="test",
            output=tmp_path,
            timeout=120,
            step_limit=0,
            slice_range=None,
            filter_regex=None,
            instance_id=None,
            workers=1,
            docker_pull_registry=None,
            use_skill=False,
            skills_dir=None,
            tokenless=False,
            per_case_prompt=False,
            prompts_dir=None,
            num_shards=2,
            shard_index=5,
        )


def test_run_help_shows_shard_options():
    result = runner.invoke(app, ["run", "--help"])

    assert result.exit_code == 0
    assert "--num-shards" in result.output
    assert "--shard-index" in result.output


def test_run_passes_shard_options_into_settings(tmp_path):
    report = RunReport(
        succeeded=1,
        failed=0,
        total=1,
        instance_ids=["inst-1"],
        metadata_path=tmp_path / "run_metadata.json",
    )

    with patch("swe_runner.cli_commands.RunSession") as mock_session_cls:
        mock_session_cls.return_value.execute.return_value = report

        result = runner.invoke(
            app,
            [
                "run",
                "--agent",
                "cosh",
                "--output",
                str(tmp_path),
                "--num-shards",
                "3",
                "--shard-index",
                "1",
            ],
        )

    assert result.exit_code == 0
    settings = mock_session_cls.call_args.args[0]
    assert settings.dataset.num_shards == 3
    assert settings.dataset.shard_index == 1
