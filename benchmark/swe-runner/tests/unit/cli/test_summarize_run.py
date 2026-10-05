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

"""Tests for the ``summarize-run`` CLI command."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from swe_runner.cli import app
from swe_runner.common.models import AgentResult, InstanceResult, Prediction, SWEInstance
from swe_runner.run.io.output_store import RunOutputStore

runner = CliRunner()

SUMMARY_COLUMNS = ["instance_id", "success", "patch_produced", "duration_seconds", "agent_name", "error"]


def _instance(instance_id: str) -> SWEInstance:
    return SWEInstance(
        instance_id=instance_id,
        repo="example/repo",
        version="1.0",
        base_commit="abc123",
        problem_statement="Fix it",
        patch="",
        test_patch="",
    )


def _failed_attempt_without_prediction() -> InstanceResult:
    return InstanceResult(
        instance=_instance("zzz__repo-2"),
        prediction=None,
        agent_result=AgentResult(
            raw_output="boom",
            patch=None,
            success=False,
            duration_seconds=12.5,
            error="agent crashed\nsecond line ✗",
            metadata={},
        ),
        success=False,
    )


def _successful_attempt_with_prediction() -> InstanceResult:
    return InstanceResult(
        instance=_instance("aaa__repo-1"),
        prediction=Prediction(instance_id="aaa__repo-1", model_name_or_path="cosh", model_patch="diff"),
        agent_result=AgentResult(
            raw_output="ok",
            patch="diff",
            success=True,
            duration_seconds=3.0,
            metadata={},
        ),
        success=True,
    )


def _read_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "summarize-run" not in path.parts
    }


def test_summarize_run_registered_and_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "summarize-run" in result.output

    result = runner.invoke(app, ["summarize-run", "--help"])
    assert result.exit_code == 0
    assert "--run-dir" in result.output
    assert "--output" in result.output


def test_summarize_run_exports_csv_from_store_written_results(tmp_path: Path) -> None:
    run_dir = tmp_path / "recorded-run"
    output_root = tmp_path / "out"
    store = RunOutputStore(run_dir)
    store.save_instance_result(_successful_attempt_with_prediction())
    store.save_instance_result(_failed_attempt_without_prediction())
    before = _snapshot_tree(run_dir)

    result = runner.invoke(
        app,
        ["summarize-run", "--run-dir", str(run_dir), "--output", str(output_root)],
    )

    assert result.exit_code == 0
    csv_path = output_root / "summarize-run" / "run_summary.csv"
    assert csv_path.exists()
    rows = _read_rows(csv_path)
    assert [row["instance_id"] for row in rows] == ["aaa__repo-1", "zzz__repo-2"]

    first, second = rows
    assert first["success"] == "true"
    assert first["patch_produced"] == "true"
    assert first["duration_seconds"] == "3.0"
    assert first["agent_name"] == "cosh"
    assert first["error"] == ""
    assert second["success"] == "false"
    assert second["patch_produced"] == "false"
    assert second["duration_seconds"] == "12.5"
    assert second["agent_name"] == ""
    assert second["error"] == "agent crashed\nsecond line ✗"
    assert _snapshot_tree(run_dir) == before


def test_summarize_run_empty_results_dir_exports_header_only(tmp_path: Path) -> None:
    run_dir = tmp_path / "recorded-run"
    (run_dir / "results").mkdir(parents=True)
    output_root = tmp_path / "out"

    result = runner.invoke(app, ["summarize-run", "--run-dir", str(run_dir), "--output", str(output_root)])

    assert result.exit_code == 0
    csv_path = output_root / "summarize-run" / "run_summary.csv"
    assert csv_path.read_text(encoding="utf-8").splitlines() == [",".join(SUMMARY_COLUMNS)]


def test_summarize_run_missing_run_dir_fails(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["summarize-run", "--run-dir", str(tmp_path / "missing"), "--output", str(tmp_path / "out")],
    )

    assert result.exit_code == 1
    assert "missing" in result.output
    assert not (tmp_path / "out" / "summarize-run" / "run_summary.csv").exists()


def test_summarize_run_malformed_sources_fail_and_preserve_previous_csv(tmp_path: Path) -> None:
    run_dir = tmp_path / "recorded-run"
    output_root = tmp_path / "out"
    store = RunOutputStore(run_dir)
    store.save_instance_result(_successful_attempt_with_prediction())
    ok = runner.invoke(app, ["summarize-run", "--run-dir", str(run_dir), "--output", str(output_root)])
    assert ok.exit_code == 0
    csv_path = output_root / "summarize-run" / "run_summary.csv"
    previous = csv_path.read_bytes()

    broken = run_dir / "results" / "broken.json"
    broken.write_text("{not-json", encoding="utf-8")
    wrong_types = run_dir / "results" / "wrong-types.json"
    wrong_types.write_text(
        json.dumps(
            {
                "instance_id": "wrong-types",
                "success": "yes",
                "error": None,
                "duration_seconds": 1.0,
                "patch_produced": True,
                "agent_name": None,
            }
        ),
        encoding="utf-8",
    )
    no_id = run_dir / "results" / "no-id.json"
    no_id.write_text(json.dumps({"success": True}), encoding="utf-8")
    mismatch = run_dir / "results" / "other-name.json"
    mismatch.write_text(
        json.dumps(
            {
                "instance_id": "aaa__repo-1",
                "success": True,
                "error": None,
                "duration_seconds": 1.0,
                "patch_produced": True,
                "agent_name": None,
            }
        ),
        encoding="utf-8",
    )

    for _ in range(4):
        failed = runner.invoke(app, ["summarize-run", "--run-dir", str(run_dir), "--output", str(output_root)])
        assert failed.exit_code == 1
        assert "broken.json" in failed.output or "wrong-types.json" in failed.output or "no-id.json" in failed.output or "other-name.json" in failed.output

    assert csv_path.read_bytes() == previous
    assert not list((output_root / "summarize-run").glob("*.tmp"))


def test_summarize_run_duplicate_instance_ids_fail(tmp_path: Path) -> None:
    run_dir = tmp_path / "recorded-run"
    results_dir = run_dir / "results"
    results_dir.mkdir(parents=True)
    payload = json.dumps(
        {
            "instance_id": "dup-1",
            "success": True,
            "error": None,
            "duration_seconds": 1.0,
            "patch_produced": False,
            "agent_name": None,
        }
    )
    (results_dir / "dup-1.json").write_text(payload, encoding="utf-8")
    (results_dir / "dup-1-copy.json").write_text(payload, encoding="utf-8")
    output_root = tmp_path / "out"

    result = runner.invoke(app, ["summarize-run", "--run-dir", str(run_dir), "--output", str(output_root)])

    assert result.exit_code == 1
    assert "dup-1" in result.output
    assert not (output_root / "summarize-run" / "run_summary.csv").exists()


def test_summarize_run_source_output_alias_keeps_sources_intact(tmp_path: Path) -> None:
    run_dir = tmp_path / "recorded-run"
    store = RunOutputStore(run_dir)
    store.save_instance_result(_successful_attempt_with_prediction())
    store.save_instance_result(_failed_attempt_without_prediction())
    before = _snapshot_tree(run_dir)

    result = runner.invoke(app, ["summarize-run", "--run-dir", str(run_dir), "--output", str(run_dir)])

    assert result.exit_code == 0
    csv_path = run_dir / "summarize-run" / "run_summary.csv"
    assert csv_path.exists()
    assert len(_read_rows(csv_path)) == 2
    assert _snapshot_tree(run_dir) == before


def test_summarize_run_publication_error_keeps_previous_csv(tmp_path: Path) -> None:
    run_dir = tmp_path / "recorded-run"
    output_root = tmp_path / "out"
    store = RunOutputStore(run_dir)
    store.save_instance_result(_successful_attempt_with_prediction())
    ok = runner.invoke(app, ["summarize-run", "--run-dir", str(run_dir), "--output", str(output_root)])
    assert ok.exit_code == 0
    csv_path = output_root / "summarize-run" / "run_summary.csv"
    previous = csv_path.read_bytes()
    store.save_instance_result(_failed_attempt_without_prediction())

    with patch(
        "swe_runner.run.io.run_summary_export.os.replace",
        side_effect=OSError("device full"),
    ):
        result = runner.invoke(app, ["summarize-run", "--run-dir", str(run_dir), "--output", str(output_root)])

    assert result.exit_code == 1
    assert "run_summary.csv" in result.output
    assert csv_path.read_bytes() == previous
    assert not list((output_root / "summarize-run").glob("*.tmp"))
