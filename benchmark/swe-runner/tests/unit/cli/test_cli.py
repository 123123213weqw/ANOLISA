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

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pytest_mock import MockerFixture
from typer.testing import CliRunner

from swe_runner.agents import AgentEnvironmentError
from swe_runner.cli import app
from swe_runner.run.io.report import RunReport

runner = CliRunner()


def test_help_shows_options():
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "--agent" in result.output
    assert "Available:" in result.output
    assert "cosh" in result.output
    assert "openclaw" in result.output
    assert "--subset" in result.output
    assert "multilingual" in result.output
    assert "--slice" in result.output
    assert "--workers" in result.output
    assert "--docker-pull-registry" in result.output
    assert "--use-skill" in result.output
    assert "--tokenless" in result.output
    assert "--no-skill" not in result.output


def test_main_help_does_not_show_extract_traces():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "extract-traces" not in result.output


def test_run_requires_agent():
    result = runner.invoke(app, ["run"])
    assert result.exit_code != 0


def test_run_invalid_agent():
    with patch(
        "swe_runner.cli_commands.RunSession.execute",
        side_effect=KeyError("Unknown agent 'nonexistent'. Available agents: cosh, openclaw"),
    ):
        result = runner.invoke(app, ["run", "--agent", "nonexistent", "--subset", "lite"])
        assert result.exit_code == 1
        assert "nonexistent" in result.output


def test_run_rejects_skill_and_per_case_prompt_together():
    result = runner.invoke(app, ["run", "--agent", "openclaw", "--use-skill", "--per-case-prompt"])

    assert result.exit_code == 1
    assert "--use-skill and --per-case-prompt are mutually exclusive" in result.output


def test_run_does_not_pass_openclaw_config_to_agent():
    with patch("swe_runner.cli_commands.RunSession") as mock_session_cls:
        mock_session_cls.return_value.execute.return_value = RunReport(succeeded=0, failed=0, total=0, instance_ids=[])

        result = runner.invoke(app, ["run", "--agent", "openclaw"])

        assert result.exit_code == 0
        settings = mock_session_cls.call_args.args[0]
        assert settings.agent.name == "openclaw"


def test_run_openclaw_env_check_failure_uses_generic_message():
    with patch(
        "swe_runner.cli_commands.RunSession.execute",
        side_effect=AgentEnvironmentError("Docker daemon is not accessible"),
    ):
        result = runner.invoke(app, ["run", "--agent", "openclaw"])

    assert result.exit_code == 1
    assert "Environment check failed" in result.output


def test_run_non_openclaw_env_check_failure_uses_generic_message():
    with patch(
        "swe_runner.cli_commands.RunSession.execute",
        side_effect=AgentEnvironmentError("Docker daemon is not accessible"),
    ):
        result = runner.invoke(app, ["run", "--agent", "cosh"])

    assert result.exit_code == 1
    assert "Environment check failed" in result.output


def test_evaluate_help():
    result = runner.invoke(app, ["evaluate", "--help"])
    assert result.exit_code == 0
    assert "predictions" in result.output
    assert "multilingual" in result.output
    assert "--namespace" in result.output


def test_evaluate_missing_predictions():
    result = runner.invoke(app, ["evaluate", "--predictions", "/nonexistent/preds.json"])
    assert result.exit_code == 1


def test_evaluate_registered():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "evaluate" in result.output


def test_analyze_traces_registered():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "analyze-traces" in result.output


def test_analyze_traces_help():
    result = runner.invoke(app, ["analyze-traces", "--help"])
    assert result.exit_code == 0
    assert "trace-root" in result.output
    assert "trim-ratio" in result.output
    assert "openclaw-profiles-dir" in result.output
    assert "--start" in result.output
    assert "--end" in result.output
    assert "--run-metadata" in result.output


def test_run_writes_run_metadata(tmp_path):
    report = RunReport(
        succeeded=1,
        failed=1,
        total=2,
        instance_ids=["inst-1", "inst-2"],
        metadata_mappings={
            "session_ids": {"inst-1": "session-inst-1"},
            "openclaw_profile_dirs": {"inst-1": "/tmp/profiles/inst-1"},
        },
        metadata_path=tmp_path / "run" / "run_metadata.json",
        started_at_ns=100,
        ended_at_ns=200,
    )

    with patch("swe_runner.cli_commands.RunSession") as mock_session_cls:
        mock_session_cls.return_value.execute.return_value = report

        result = runner.invoke(app, ["run", "--agent", "openclaw", "--output", str(tmp_path)])

    assert result.exit_code == 0
    assert "1/2 succeeded" in result.output
    assert "Run metadata" in result.output
    settings = mock_session_cls.call_args.args[0]
    assert settings.agent.name == "openclaw"
    assert settings.agent.workers == 1
    assert settings.output.output_dir == tmp_path / "run"


def test_run_passes_use_skill_into_settings(tmp_path):
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
                "--use-skill",
                "--skills-dir",
                str(tmp_path / "skills"),
            ],
        )

    assert result.exit_code == 0
    settings = mock_session_cls.call_args.args[0]
    assert settings.agent.use_skill is True
    assert settings.agent.skills_dir == tmp_path / "skills"


def test_run_passes_prompts_dir_into_settings(tmp_path):
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
                "--per-case-prompt",
                "--prompts-dir",
                str(tmp_path / "prompts"),
            ],
        )

    assert result.exit_code == 0
    settings = mock_session_cls.call_args.args[0]
    assert settings.agent.per_case_prompt is True
    assert settings.agent.prompts_dir == tmp_path / "prompts"


def test_run_passes_tokenless_into_settings(tmp_path):
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
                "openclaw",
                "--output",
                str(tmp_path),
                "--tokenless",
            ],
        )

    assert result.exit_code == 0
    settings = mock_session_cls.call_args.args[0]
    assert settings.agent.tokenless is True


def test_run_rejects_tokenless_for_unsupported_agent() -> None:
    result = runner.invoke(app, ["run", "--agent", "cosh", "--tokenless"])

    assert result.exit_code == 1
    assert "--tokenless is not supported by agent 'cosh'" in result.output


def test_analyze_traces_can_collect_from_openclaw_jsonl(tmp_path):
    metadata_path = tmp_path / "run_metadata.json"
    profiles_dir = tmp_path / "openclaw-profiles"
    fake_plan = SimpleNamespace(
        should_collect=True,
        collect=lambda trace_root: [tmp_path / "traces" / "inst" / "trace1.json"],
    )

    with (
        patch(
            "swe_runner.cli_commands.TraceCollectionPlan.resolve",
            return_value=fake_plan,
        ) as mock_resolve,
        patch(
            "swe_runner.cli_commands.write_trace_analysis_csvs",
            return_value=(tmp_path / "detail", tmp_path / "summary.csv"),
        ),
    ):
        result = runner.invoke(
            app,
            [
                "analyze-traces",
                "--run-metadata",
                str(metadata_path),
                "--openclaw-profiles-dir",
                str(profiles_dir),
            ],
        )

    assert result.exit_code == 0
    mock_resolve.assert_called_once_with(
        start=None,
        end="now",
        run_metadata_path=metadata_path,
        openclaw_profiles_dir=profiles_dir,
    )


def test_analyze_traces_collects_openclaw_jsonl_by_session_ids_and_profile_dirs(tmp_path):
    metadata_path = tmp_path / "run_metadata.json"
    fake_plan = SimpleNamespace(
        should_collect=True,
        collect=lambda trace_root: [tmp_path / "traces" / "inst" / "trace1.json"],
    )

    with (
        patch(
            "swe_runner.cli_commands.TraceCollectionPlan.resolve",
            return_value=fake_plan,
        ) as mock_resolve,
        patch(
            "swe_runner.cli_commands.write_trace_analysis_csvs",
            return_value=(tmp_path / "detail", tmp_path / "summary.csv"),
        ),
    ):
        result = runner.invoke(
            app,
            [
                "analyze-traces",
                "--run-metadata",
                str(metadata_path),
            ],
        )

    assert result.exit_code == 0
    mock_resolve.assert_called_once_with(
        start=None,
        end="now",
        run_metadata_path=metadata_path,
        openclaw_profiles_dir=None,
    )


def test_evaluate_success_mock(mocker: MockerFixture) -> None:
    """Test evaluate command with mocked evaluation module."""
    from swe_runner.evaluation import EvalInstanceResult, EvalReport

    mock_report = EvalReport(
        total_instances=1,
        resolved_full=1,
        resolved_partial=0,
        resolved_no=0,
        patch_failed=0,
        error_count=0,
        resolution_rate=1.0,
        instance_results=[
            EvalInstanceResult(
                instance_id="test-1", resolved=True, resolution_status="RESOLVED_FULL", patch_applied=True
            )
        ],
        run_id="test",
        dataset_name="princeton-nlp/SWE-bench_Lite",
        evaluated_at="2026-01-01T00:00:00Z",
    )
    mock_run_evaluation = mocker.patch("swe_runner.cli_commands.run_patch_evaluation", return_value=mock_report)

    # Create a dummy preds.json so the file-exists check passes
    import json
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"i1": {"instance_id": "i1", "model_name_or_path": "cosh", "model_patch": "diff"}}, f)
        preds_path = f.name

    result = runner.invoke(app, ["evaluate", "--predictions", preds_path])
    assert result.exit_code == 0
    mock_run_evaluation.assert_called_once()
    assert mock_run_evaluation.call_args.args[1] == Path("output/evaluate")


def test_evaluate_none_namespace_uses_local_build_mode(mocker: MockerFixture) -> None:
    mock_run_evaluation = mocker.patch("swe_runner.cli_commands.run_patch_evaluation", return_value=None)

    import json
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"i1": {"instance_id": "i1", "model_name_or_path": "cosh", "model_patch": "diff"}}, f)
        preds_path = f.name

    result = runner.invoke(app, ["evaluate", "--predictions", preds_path, "--namespace", "none"])

    assert result.exit_code == 0
    mock_run_evaluation.assert_called_once()
    assert mock_run_evaluation.call_args.kwargs["namespace"] is None


def _write_compare_summary(path, rows):
    import csv

    headers = ["用例ID", "执行次数", "平均执行步骤数", "平均输入Token数", "平均输出Token数", "平均总Token数"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow({header: row.get(header, "") for header in headers})
    return path


def test_compare_traces_registered():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "compare-traces" in result.output


def test_compare_traces_help():
    result = runner.invoke(app, ["compare-traces", "--help"])
    assert result.exit_code == 0
    assert "--baseline" in result.output
    assert "--candidate" in result.output
    assert "--output" in result.output


def test_compare_traces_dispatch_writes_alignment_report(tmp_path):
    baseline = _write_compare_summary(
        tmp_path / "base.csv",
        [
            {"用例ID": "inst-a", "执行次数": 2, "平均执行步骤数": "12.00", "平均输入Token数": "110.00", "平均输出Token数": "40.00", "平均总Token数": "150.00"},
            {"用例ID": "inst-b", "执行次数": 1, "平均执行步骤数": "5.00", "平均输入Token数": "200.00", "平均输出Token数": "100.00", "平均总Token数": "300.00"},
        ],
    )
    candidate = _write_compare_summary(
        tmp_path / "cand.csv",
        [
            {"用例ID": "inst-a", "执行次数": 1, "平均执行步骤数": "6.00", "平均输入Token数": "55.00", "平均输出Token数": "25.00", "平均总Token数": "80.00"},
            {"用例ID": "inst-c", "执行次数": 1, "平均执行步骤数": "2.00", "平均输入Token数": "10.00", "平均输出Token数": "5.00", "平均总Token数": "15.00"},
        ],
    )

    result = runner.invoke(
        app,
        [
            "compare-traces",
            "--baseline",
            str(baseline),
            "--candidate",
            str(candidate),
            "--output",
            str(tmp_path / "out"),
        ],
    )

    assert result.exit_code == 0
    assert "Matched cases: 1" in result.output
    assert "Baseline-only cases: 1" in result.output
    assert "Candidate-only cases: 1" in result.output

    output_csv = tmp_path / "out" / "compare-traces" / "trace_comparison.csv"
    assert output_csv.is_file()

    import csv

    with open(output_csv, encoding="utf-8", newline="") as f:
        rows = {row["用例ID"]: row for row in csv.DictReader(f)}
    assert rows["inst-a"]["匹配状态"] == "matched"
    assert rows["inst-a"]["平均总Token数差异"] == "-70.00"
    assert rows["inst-b"]["匹配状态"] == "baseline-only"
    assert rows["inst-b"]["平均总Token数差异"] == ""
    assert rows["inst-c"]["匹配状态"] == "candidate-only"


def test_compare_traces_missing_baseline_exits_with_error(tmp_path):
    candidate = _write_compare_summary(tmp_path / "cand.csv", [{"用例ID": "inst-a", "执行次数": 1}])

    result = runner.invoke(
        app,
        [
            "compare-traces",
            "--baseline",
            str(tmp_path / "missing.csv"),
            "--candidate",
            str(candidate),
            "--output",
            str(tmp_path / "out"),
        ],
    )

    assert result.exit_code == 1
    assert "Baseline trace summary not found" in result.output


def test_compare_traces_invalid_rows_exit_before_output(tmp_path):
    baseline = _write_compare_summary(
        tmp_path / "base.csv",
        [{"用例ID": "inst-a", "执行次数": 1, "平均执行步骤数": "10.00", "平均输入Token数": "100.00", "平均输出Token数": "50.00", "平均总Token数": "150.00"}],
    )
    candidate = _write_compare_summary(
        tmp_path / "cand.csv",
        [
            {"用例ID": "inst-a", "执行次数": 1, "平均执行步骤数": "10.00", "平均输入Token数": "100.00", "平均输出Token数": "50.00", "平均总Token数": "150.00"},
            {"用例ID": "inst-a", "执行次数": 2, "平均执行步骤数": "8.00", "平均输入Token数": "60.00", "平均输出Token数": "30.00", "平均总Token数": "90.00"},
        ],
    )

    result = runner.invoke(
        app,
        [
            "compare-traces",
            "--baseline",
            str(baseline),
            "--candidate",
            str(candidate),
            "--output",
            str(tmp_path / "out"),
        ],
    )

    assert result.exit_code == 1
    assert "repeats case ID 'inst-a'" in result.output
    assert not (tmp_path / "out" / "compare-traces" / "trace_comparison.csv").exists()
