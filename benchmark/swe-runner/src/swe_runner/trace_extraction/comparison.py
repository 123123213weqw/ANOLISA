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

"""Alignment of two exported trace summary CSVs across runs."""

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from swe_runner.trace_extraction.helpers import ExtractionError, _format_metric

STATUS_MATCHED = "matched"
STATUS_BASELINE_ONLY = "baseline-only"
STATUS_CANDIDATE_ONLY = "candidate-only"

_SUMMARY_SOURCE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("用例ID", "instance_id"),
    ("执行次数", "execution_count"),
    ("平均执行步骤数", "avg_steps"),
    ("平均输入Token数", "avg_input_tokens"),
    ("平均输出Token数", "avg_output_tokens"),
    ("平均总Token数", "avg_total_tokens"),
)

_COMPARED_METRICS: tuple[tuple[str, str], ...] = (
    ("平均执行步骤数", "avg_steps"),
    ("平均输入Token数", "avg_input_tokens"),
    ("平均输出Token数", "avg_output_tokens"),
    ("平均总Token数", "avg_total_tokens"),
)

_COMPARISON_COLUMNS: tuple[tuple[str, str], ...] = (
    ("用例ID", "instance_id"),
    ("匹配状态", "match_status"),
    ("基线执行次数", "baseline_execution_count"),
    ("候选执行次数", "candidate_execution_count"),
    ("基线平均执行步骤数", "baseline_avg_steps"),
    ("候选平均执行步骤数", "candidate_avg_steps"),
    ("基线平均输入Token数", "baseline_avg_input_tokens"),
    ("候选平均输入Token数", "candidate_avg_input_tokens"),
    ("基线平均输出Token数", "baseline_avg_output_tokens"),
    ("候选平均输出Token数", "candidate_avg_output_tokens"),
    ("基线平均总Token数", "baseline_avg_total_tokens"),
    ("候选平均总Token数", "candidate_avg_total_tokens"),
    ("平均执行步骤数差异", "avg_steps_diff"),
    ("平均输入Token数差异", "avg_input_tokens_diff"),
    ("平均输出Token数差异", "avg_output_tokens_diff"),
    ("平均总Token数差异", "avg_total_tokens_diff"),
    ("平均执行步骤数变化百分比", "avg_steps_pct_change"),
    ("平均输入Token数变化百分比", "avg_input_tokens_pct_change"),
    ("平均输出Token数变化百分比", "avg_output_tokens_pct_change"),
    ("平均总Token数变化百分比", "avg_total_tokens_pct_change"),
)


@dataclass(frozen=True)
class TraceSummaryRecord:
    """One validated per-case row of an exported trace summary CSV."""

    instance_id: str
    execution_count: int
    metrics: dict[str, float]


@dataclass(frozen=True)
class TraceComparisonResult:
    """Counts and output produced by a trace summary comparison."""

    output_csv: Path
    matched_count: int
    baseline_only_count: int
    candidate_only_count: int


def _parse_finite_metric(value: Any, path: Path, row_number: int, column: str) -> float:
    """Parse one summary metric, rejecting missing, non-numeric and nonfinite values."""
    if not isinstance(value, str) or not value.strip():
        raise ExtractionError(f"{path}: row {row_number} has an empty {column} value")
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ExtractionError(f"{path}: row {row_number} has a non-numeric {column} value: {value!r}") from exc
    if not math.isfinite(parsed):
        raise ExtractionError(f"{path}: row {row_number} has a nonfinite {column} value: {value!r}")
    if parsed < 0:
        raise ExtractionError(f"{path}: row {row_number} has a negative {column} value: {value!r}")
    return parsed


def _load_summary_records(summary_csv: Path) -> dict[str, TraceSummaryRecord]:
    """Read one exported trace summary CSV into validated per-case records."""
    if not summary_csv.is_file():
        raise ExtractionError(f"Trace summary not found: {summary_csv}")

    try:
        with open(summary_csv, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            if fieldnames is None:
                raise ExtractionError(f"{summary_csv}: trace summary is empty")
            missing_columns = [header for header, _ in _SUMMARY_SOURCE_COLUMNS if header not in fieldnames]
            if missing_columns:
                raise ExtractionError(
                    f"{summary_csv}: trace summary is missing required columns: {', '.join(missing_columns)}"
                )
            raw_rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise ExtractionError(f"{summary_csv}: failed to read trace summary: {exc}") from exc

    records: dict[str, TraceSummaryRecord] = {}
    for row_number, row in enumerate(raw_rows, start=2):
        if row.get(None):
            raise ExtractionError(f"{summary_csv}: row {row_number} has more fields than the header")
        instance_id = row.get("用例ID")
        if not isinstance(instance_id, str) or not instance_id:
            raise ExtractionError(f"{summary_csv}: row {row_number} has an empty case ID")
        if instance_id in records:
            raise ExtractionError(f"{summary_csv}: row {row_number} repeats case ID {instance_id!r}")
        execution_count_metric = _parse_finite_metric(row.get("执行次数"), summary_csv, row_number, "执行次数")
        if not execution_count_metric.is_integer():
            raise ExtractionError(
                f"{summary_csv}: row {row_number} has a non-integer execution count: {row.get('执行次数')!r}"
            )
        metrics = {
            key: _parse_finite_metric(row.get(header), summary_csv, row_number, header)
            for header, key in _COMPARED_METRICS
        }
        records[instance_id] = TraceSummaryRecord(
            instance_id=instance_id,
            execution_count=int(execution_count_metric),
            metrics=metrics,
        )
    return records


def _comparison_row(
    *,
    instance_id: str,
    status: str,
    baseline: TraceSummaryRecord | None,
    candidate: TraceSummaryRecord | None,
) -> dict[str, str | int]:
    """Build one localized comparison row keyed by Chinese output headers."""
    row: dict[str, str | int] = {header: "" for header, _ in _COMPARISON_COLUMNS}
    row["用例ID"] = instance_id
    row["匹配状态"] = status
    if baseline is not None:
        row["基线执行次数"] = baseline.execution_count
        for header, key in _COMPARED_METRICS:
            row[f"基线{header}"] = _format_metric(baseline.metrics[key])
    if candidate is not None:
        row["候选执行次数"] = candidate.execution_count
        for header, key in _COMPARED_METRICS:
            row[f"候选{header}"] = _format_metric(candidate.metrics[key])
    if baseline is not None and candidate is not None:
        for header, key in _COMPARED_METRICS:
            baseline_value = baseline.metrics[key]
            candidate_value = candidate.metrics[key]
            difference = candidate_value - baseline_value
            row[f"{header}差异"] = _format_metric(difference)
            if baseline_value > 0:
                row[f"{header}变化百分比"] = _format_metric(difference / baseline_value * 100)
            elif candidate_value == 0:
                row[f"{header}变化百分比"] = _format_metric(0.0)
    return row


def compare_trace_summaries(
    baseline_csv: str | Path,
    candidate_csv: str | Path,
    output_csv: str | Path,
) -> TraceComparisonResult:
    """Align two exported trace summary CSVs by exact case ID and write the comparison CSV."""
    baseline_path = Path(baseline_csv)
    candidate_path = Path(candidate_csv)
    output_path = Path(output_csv)

    baseline_records = _load_summary_records(baseline_path)
    candidate_records = _load_summary_records(candidate_path)

    matched_count = 0
    baseline_only_count = 0
    candidate_only_count = 0
    rows: list[dict[str, str | int]] = []
    for instance_id in sorted(set(baseline_records) | set(candidate_records)):
        baseline = baseline_records.get(instance_id)
        candidate = candidate_records.get(instance_id)
        if baseline is not None and candidate is not None:
            status = STATUS_MATCHED
            matched_count += 1
        elif baseline is not None:
            status = STATUS_BASELINE_ONLY
            baseline_only_count += 1
        else:
            status = STATUS_CANDIDATE_ONLY
            candidate_only_count += 1
        rows.append(
            _comparison_row(instance_id=instance_id, status=status, baseline=baseline, candidate=candidate)
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[header for header, _ in _COMPARISON_COLUMNS])
        writer.writeheader()
        writer.writerows(rows)

    return TraceComparisonResult(
        output_csv=output_path,
        matched_count=matched_count,
        baseline_only_count=baseline_only_count,
        candidate_only_count=candidate_only_count,
    )
