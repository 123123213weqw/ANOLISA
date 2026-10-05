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

"""Tests for cross-run comparison of exported trace summary CSVs."""

import csv
import json
from pathlib import Path

import pytest

from swe_runner.trace_extraction import (
    ExtractionError,
    compare_trace_summaries,
    write_trace_analysis_csvs,
)

_SUMMARY_HEADERS = [
    "用例ID",
    "执行次数",
    "平均执行步骤数",
    "最小执行步骤数",
    "最大执行步骤数",
    "平均输入Token数",
    "平均输出Token数",
    "平均总Token数",
    "截尾平均输入Token数",
    "截尾平均输出Token数",
    "截尾平均总Token数",
    "最小总Token数",
    "最大总Token数",
]


def _summary_row(
    instance_id: str,
    execution_count: int | str = 1,
    avg_steps: str = "10.00",
    avg_input: str = "100.00",
    avg_output: str = "50.00",
    avg_total: str = "150.00",
) -> dict[str, str | int]:
    return {
        "用例ID": instance_id,
        "执行次数": execution_count,
        "平均执行步骤数": avg_steps,
        "平均输入Token数": avg_input,
        "平均输出Token数": avg_output,
        "平均总Token数": avg_total,
    }


def _write_summary(path: Path, rows: list[dict[str, str | int]], headers: list[str] | None = None) -> Path:
    fieldnames = headers if headers is not None else _SUMMARY_HEADERS
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({header: row.get(header, "") for header in fieldnames})
    return path


def _read_rows(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _write_trace(root: Path, instance_id: str, name: str, payload: dict[str, object]) -> Path:
    instance_dir = root / instance_id
    instance_dir.mkdir(parents=True, exist_ok=True)
    trace_file = instance_dir / name
    trace_file.write_text(json.dumps(payload), encoding="utf-8")
    return trace_file


class TestCompareTraceSummaries:
    def test_end_to_end_with_exported_summaries(self, tmp_path):
        baseline_root = tmp_path / "baseline" / "traces"
        _write_trace(baseline_root, "inst-a", "trace1.json", {"total_input_tokens": 100, "total_output_tokens": 50, "total_steps": 10})
        _write_trace(baseline_root, "inst-a", "trace2.json", {"total_input_tokens": 120, "total_output_tokens": 30, "total_steps": 14})
        _write_trace(baseline_root, "inst-b", "trace1.json", {"total_input_tokens": 200, "total_output_tokens": 100, "total_steps": 5})
        baseline_summary = write_trace_analysis_csvs(baseline_root, tmp_path / "baseline")[1]

        candidate_root = tmp_path / "candidate" / "traces"
        _write_trace(candidate_root, "inst-a", "trace1.json", {"total_input_tokens": 55, "total_output_tokens": 25, "total_steps": 6})
        _write_trace(candidate_root, "inst-c", "trace1.json", {"total_input_tokens": 10, "total_output_tokens": 5, "total_steps": 2})
        candidate_summary = write_trace_analysis_csvs(candidate_root, tmp_path / "candidate")[1]

        output_csv = tmp_path / "compare" / "trace_comparison.csv"
        result = compare_trace_summaries(baseline_summary, candidate_summary, output_csv)

        assert (result.matched_count, result.baseline_only_count, result.candidate_only_count) == (1, 1, 1)
        rows = _read_rows(output_csv)
        assert [row["用例ID"] for row in rows] == ["inst-a", "inst-b", "inst-c"]

        inst_a = rows[0]
        assert inst_a["匹配状态"] == "matched"
        assert inst_a["基线执行次数"] == "2"
        assert inst_a["候选执行次数"] == "1"
        assert inst_a["基线平均输入Token数"] == "110.00"
        assert inst_a["候选平均输入Token数"] == "55.00"
        assert inst_a["平均输入Token数差异"] == "-55.00"
        assert inst_a["平均输入Token数变化百分比"] == "-50.00"
        assert inst_a["基线平均总Token数"] == "150.00"
        assert inst_a["候选平均总Token数"] == "80.00"
        assert inst_a["平均总Token数差异"] == "-70.00"
        assert inst_a["平均总Token数变化百分比"] == "-46.67"
        assert inst_a["基线平均执行步骤数"] == "12.00"
        assert inst_a["候选平均执行步骤数"] == "6.00"

        inst_b = rows[1]
        assert inst_b["匹配状态"] == "baseline-only"
        assert inst_b["基线执行次数"] == "1"
        assert inst_b["候选执行次数"] == ""
        assert inst_b["候选平均输入Token数"] == ""
        assert inst_b["平均输入Token数差异"] == ""
        assert inst_b["平均输入Token数变化百分比"] == ""

        inst_c = rows[2]
        assert inst_c["匹配状态"] == "candidate-only"
        assert inst_c["基线执行次数"] == ""
        assert inst_c["基线平均输出Token数"] == ""
        assert inst_c["平均输出Token数差异"] == ""

    def test_zero_baseline_with_positive_candidate_leaves_percentage_blank(self, tmp_path):
        baseline = _write_summary(
            tmp_path / "base.csv",
            [_summary_row("inst-a", avg_input="0.00", avg_total="0.00")],
        )
        candidate = _write_summary(
            tmp_path / "cand.csv",
            [_summary_row("inst-a", avg_input="80.00", avg_steps="4.00", avg_output="20.00", avg_total="100.00")],
        )
        output_csv = tmp_path / "out" / "trace_comparison.csv"

        result = compare_trace_summaries(baseline, candidate, output_csv)

        assert result.matched_count == 1
        row = _read_rows(output_csv)[0]
        assert row["平均输入Token数差异"] == "80.00"
        assert row["平均输入Token数变化百分比"] == ""
        assert row["平均总Token数差异"] == "100.00"
        assert row["平均总Token数变化百分比"] == ""

    def test_zero_baseline_with_zero_candidate_reports_zero_change(self, tmp_path):
        baseline = _write_summary(tmp_path / "base.csv", [_summary_row("inst-a", avg_input="0.00")])
        candidate = _write_summary(tmp_path / "cand.csv", [_summary_row("inst-a", avg_input="0.00")])
        output_csv = tmp_path / "out" / "trace_comparison.csv"

        compare_trace_summaries(baseline, candidate, output_csv)

        row = _read_rows(output_csv)[0]
        assert row["平均输入Token数差异"] == "0.00"
        assert row["平均输入Token数变化百分比"] == "0.00"

    def test_unicode_and_quoted_case_ids_align(self, tmp_path):
        quoted_id = 'django__django-1,2'
        unicode_id = '用例"引号"'
        plain_id = "scikit-learn__scikit-learn-999"
        baseline = _write_summary(
            tmp_path / "base.csv",
            [_summary_row(quoted_id), _summary_row(unicode_id), _summary_row(plain_id)],
        )
        candidate = _write_summary(
            tmp_path / "cand.csv",
            [_summary_row(plain_id, avg_input="80.00"), _summary_row(quoted_id, avg_input="60.00"), _summary_row(unicode_id)],
        )
        output_csv = tmp_path / "out" / "trace_comparison.csv"

        result = compare_trace_summaries(baseline, candidate, output_csv)

        assert result.matched_count == 3
        rows = {row["用例ID"]: row for row in _read_rows(output_csv)}
        assert set(rows) == {quoted_id, unicode_id, plain_id}
        assert rows[plain_id]["平均输入Token数差异"] == "-20.00"
        assert rows[quoted_id]["平均输入Token数变化百分比"] == "-40.00"

    def test_output_rows_are_sorted_by_case_id(self, tmp_path):
        baseline = _write_summary(
            tmp_path / "base.csv",
            [_summary_row("case-c"), _summary_row("case-a"), _summary_row("case-b")],
        )
        candidate = _write_summary(
            tmp_path / "cand.csv",
            [_summary_row("case-b"), _summary_row("case-c"), _summary_row("case-a")],
        )
        output_csv = tmp_path / "out" / "trace_comparison.csv"

        compare_trace_summaries(baseline, candidate, output_csv)

        assert [row["用例ID"] for row in _read_rows(output_csv)] == ["case-a", "case-b", "case-c"]

    def test_empty_summaries_produce_header_only_output(self, tmp_path):
        baseline = _write_summary(tmp_path / "base.csv", [])
        candidate = _write_summary(tmp_path / "cand.csv", [])
        output_csv = tmp_path / "out" / "trace_comparison.csv"

        result = compare_trace_summaries(baseline, candidate, output_csv)

        assert (result.matched_count, result.baseline_only_count, result.candidate_only_count) == (0, 0, 0)
        rows = _read_rows(output_csv)
        assert rows == []
        with open(output_csv, encoding="utf-8", newline="") as f:
            assert f.readline().rstrip("\r\n") == ",".join(
                [
                    "用例ID",
                    "匹配状态",
                    "基线执行次数",
                    "候选执行次数",
                    "基线平均执行步骤数",
                    "候选平均执行步骤数",
                    "基线平均输入Token数",
                    "候选平均输入Token数",
                    "基线平均输出Token数",
                    "候选平均输出Token数",
                    "基线平均总Token数",
                    "候选平均总Token数",
                    "平均执行步骤数差异",
                    "平均输入Token数差异",
                    "平均输出Token数差异",
                    "平均总Token数差异",
                    "平均执行步骤数变化百分比",
                    "平均输入Token数变化百分比",
                    "平均输出Token数变化百分比",
                    "平均总Token数变化百分比",
                ]
            )

    def test_rejects_duplicate_case_ids_before_writing_output(self, tmp_path):
        baseline = _write_summary(tmp_path / "base.csv", [_summary_row("inst-a")])
        candidate = _write_summary(
            tmp_path / "cand.csv",
            [_summary_row("inst-a"), _summary_row("inst-a", avg_input="120.00")],
        )
        output_csv = tmp_path / "out" / "trace_comparison.csv"

        with pytest.raises(ExtractionError, match="repeats case ID 'inst-a'"):
            compare_trace_summaries(baseline, candidate, output_csv)

        assert not output_csv.exists()

    @pytest.mark.parametrize("value", ["NaN", "inf", "-Infinity"])
    def test_rejects_nonfinite_values(self, tmp_path, value):
        baseline = _write_summary(tmp_path / "base.csv", [_summary_row("inst-a", avg_output=value)])
        candidate = _write_summary(tmp_path / "cand.csv", [_summary_row("inst-a")])
        output_csv = tmp_path / "out" / "trace_comparison.csv"

        with pytest.raises(ExtractionError, match="nonfinite"):
            compare_trace_summaries(baseline, candidate, output_csv)

        assert not output_csv.exists()

    @pytest.mark.parametrize(
        ("row", "message"),
        [
            (_summary_row("inst-a", avg_steps="many"), "non-numeric"),
            (_summary_row("inst-a", avg_input=""), "empty"),
            (_summary_row("", avg_input="10.00"), "empty case ID"),
            (_summary_row("inst-a", avg_input="-5.00"), "negative"),
            (_summary_row("inst-a", execution_count="1.5"), "non-integer execution count"),
        ],
    )
    def test_rejects_malformed_rows(self, tmp_path, row, message):
        baseline = _write_summary(tmp_path / "base.csv", [row])
        candidate = _write_summary(tmp_path / "cand.csv", [_summary_row("inst-a")])
        output_csv = tmp_path / "out" / "trace_comparison.csv"

        with pytest.raises(ExtractionError, match=message):
            compare_trace_summaries(baseline, candidate, output_csv)

        assert not output_csv.exists()

    def test_rejects_summary_missing_required_column(self, tmp_path):
        headers = [header for header in _SUMMARY_HEADERS if header != "平均总Token数"]
        baseline = _write_summary(tmp_path / "base.csv", [], headers=headers)
        candidate = _write_summary(tmp_path / "cand.csv", [])
        output_csv = tmp_path / "out" / "trace_comparison.csv"

        with pytest.raises(ExtractionError, match="missing required columns: 平均总Token数"):
            compare_trace_summaries(baseline, candidate, output_csv)

    def test_rejects_missing_input_file(self, tmp_path):
        candidate = _write_summary(tmp_path / "cand.csv", [_summary_row("inst-a")])
        output_csv = tmp_path / "out" / "trace_comparison.csv"

        with pytest.raises(ExtractionError, match="not found"):
            compare_trace_summaries(tmp_path / "missing.csv", candidate, output_csv)
