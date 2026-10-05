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

"""Export the recorded run outcome inventory to a deterministic CSV."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

RESULTS_DIR_NAME = "results"
SUMMARY_DIR_NAME = "summarize-run"
SUMMARY_CSV_NAME = "run_summary.csv"
SUMMARY_COLUMNS = (
    "instance_id",
    "success",
    "patch_produced",
    "duration_seconds",
    "agent_name",
    "error",
)


class RunSummaryExportError(ValueError):
    """Raised when a recorded run cannot be exported to the summary CSV."""


@dataclass(frozen=True)
class RunSummaryExportResult:
    """Outcome of one run summary export."""

    attempt_count: int
    summary_csv: Path


@dataclass(frozen=True)
class _OutcomeRow:
    """One validated row of the run outcome inventory."""

    instance_id: str
    success: bool
    patch_produced: bool
    duration_seconds: float | int | None
    agent_name: str | None
    error: str | None


def _require_field(path: Path, data: dict[str, Any], field: str) -> Any:
    if field not in data:
        raise RunSummaryExportError(f"Malformed result file {path}: missing required field '{field}'")
    return data[field]


def _load_outcome(path: Path) -> _OutcomeRow:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RunSummaryExportError(f"Malformed result file {path}: invalid JSON ({exc})") from exc
    except OSError as exc:
        raise RunSummaryExportError(f"Cannot read result file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RunSummaryExportError(f"Malformed result file {path}: expected a JSON object")

    instance_id = _require_field(path, data, "instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        raise RunSummaryExportError(f"Malformed result file {path}: 'instance_id' must be a non-empty string")
    if path.stem != instance_id:
        raise RunSummaryExportError(
            f"Malformed result file {path}: records instance_id '{instance_id}' which does not match the file name"
        )

    success = _require_field(path, data, "success")
    if not isinstance(success, bool):
        raise RunSummaryExportError(f"Malformed result file {path}: 'success' must be a boolean")
    patch_produced = _require_field(path, data, "patch_produced")
    if not isinstance(patch_produced, bool):
        raise RunSummaryExportError(f"Malformed result file {path}: 'patch_produced' must be a boolean")

    duration_seconds = _require_field(path, data, "duration_seconds")
    duration_is_number = isinstance(duration_seconds, (int, float)) and not isinstance(duration_seconds, bool)
    if duration_seconds is not None and not duration_is_number:
        raise RunSummaryExportError(f"Malformed result file {path}: 'duration_seconds' must be a number or null")

    agent_name = _require_field(path, data, "agent_name")
    if agent_name is not None and not isinstance(agent_name, str):
        raise RunSummaryExportError(f"Malformed result file {path}: 'agent_name' must be a string or null")
    error = _require_field(path, data, "error")
    if error is not None and not isinstance(error, str):
        raise RunSummaryExportError(f"Malformed result file {path}: 'error' must be a string or null")

    return _OutcomeRow(
        instance_id=instance_id,
        success=success,
        patch_produced=patch_produced,
        duration_seconds=duration_seconds,
        agent_name=agent_name,
        error=error,
    )


def _format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _render_csv(outcomes: list[_OutcomeRow]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(SUMMARY_COLUMNS)
    for outcome in outcomes:
        writer.writerow(
            [
                _format_cell(outcome.instance_id),
                _format_cell(outcome.success),
                _format_cell(outcome.patch_produced),
                _format_cell(outcome.duration_seconds),
                _format_cell(outcome.agent_name),
                _format_cell(outcome.error),
            ]
        )
    return buffer.getvalue()


def export_run_summary(run_dir: Path, output_root: Path) -> RunSummaryExportResult:
    """Validate the results recorded under ``run_dir`` and publish the summary CSV atomically."""
    resolved_run_dir = run_dir.resolve()
    if not resolved_run_dir.exists():
        raise RunSummaryExportError(f"Run directory not found: {run_dir}")
    if not resolved_run_dir.is_dir():
        raise RunSummaryExportError(f"Run directory is not a directory: {run_dir}")

    results_dir = resolved_run_dir / RESULTS_DIR_NAME
    outcomes: list[_OutcomeRow] = []
    seen: dict[str, Path] = {}
    result_files = sorted(results_dir.glob("*.json")) if results_dir.is_dir() else []
    for result_file in result_files:
        outcome = _load_outcome(result_file)
        previous = seen.get(outcome.instance_id)
        if previous is not None:
            raise RunSummaryExportError(
                f"Duplicate instance ID '{outcome.instance_id}' in result files {previous} and {result_file}"
            )
        seen[outcome.instance_id] = result_file
        outcomes.append(outcome)
    outcomes.sort(key=lambda outcome: outcome.instance_id)

    summary_csv = output_root.resolve() / SUMMARY_DIR_NAME / SUMMARY_CSV_NAME
    summary_csv.parent.mkdir(parents=True, exist_ok=True)

    payload = _render_csv(outcomes)
    fd, tmp_name = tempfile.mkstemp(dir=summary_csv.parent, prefix=".run_summary.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, summary_csv)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise RunSummaryExportError(f"Failed to publish run summary CSV {summary_csv}: {exc}") from exc

    logger.info(
        "SUMMARIZE_RUN_EXPORT attempts=%s run_dir=%s summary_csv=%s",
        len(outcomes),
        resolved_run_dir,
        summary_csv,
    )
    return RunSummaryExportResult(attempt_count=len(outcomes), summary_csv=summary_csv)
