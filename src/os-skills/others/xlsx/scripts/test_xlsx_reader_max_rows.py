#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for xlsx_reader.py per-sheet row budget (--max-rows).

Regression tests for the row-budget feature: pushing N+1 into pandas IO to
bound materialization, analyzing at most N rows per sheet/record set, and
describing the analyzed-row scope, per-sheet truncation, and loaded row
counts truthfully in JSON and human reports.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(SCRIPTS_DIR, "xlsx_reader.py")
sys.path.insert(0, SCRIPTS_DIR)

import xlsx_reader  # noqa: E402


def _write_workbook(path, sheets):
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(row)
    wb.save(path)


def _write_text(path, text, encoding="utf-8"):
    with open(path, "w", encoding=encoding, newline="") as f:
        f.write(text)


class _TempFileMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def path(self, name):
        return os.path.join(self.dir, name)


class RowBudgetApiTests(_TempFileMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.xlsx_path = self.path("book.xlsx")
        _write_workbook(
            self.xlsx_path,
            {
                "Sheet1": [["id", "value"]] + [[i, i * 10] for i in range(1, 6)],
                "Sheet2": [["id", "value"]] + [[i, i * 10] for i in range(1, 3)],
            },
        )

    def test_excel_budget_limits_each_sheet(self):
        sheets = xlsx_reader.detect_and_load(self.xlsx_path, max_rows=2)

        self.assertEqual(set(sheets), {"Sheet1", "Sheet2"})
        self.assertEqual(len(sheets["Sheet1"]), 2)
        self.assertEqual(len(sheets["Sheet2"]), 2)
        structure = xlsx_reader.explore_structure(sheets)
        self.assertEqual(
            structure["Sheet1"]["row_scope"],
            {"max_rows": 2, "loaded_rows": 3, "analyzed_rows": 2, "truncated": True},
        )
        self.assertEqual(
            structure["Sheet2"]["row_scope"],
            {"max_rows": 2, "loaded_rows": 2, "analyzed_rows": 2, "truncated": False},
        )
        self.assertEqual(structure["Sheet1"]["shape"]["rows"], 2)

    def test_excel_named_sheet_budget(self):
        sheets = xlsx_reader.detect_and_load(self.xlsx_path, sheet_name_filter="Sheet2", max_rows=1)

        self.assertEqual(set(sheets), {"Sheet2"})
        self.assertEqual(len(sheets["Sheet2"]), 1)
        structure = xlsx_reader.explore_structure(sheets)
        self.assertEqual(
            structure["Sheet2"]["row_scope"],
            {"max_rows": 1, "loaded_rows": 2, "analyzed_rows": 1, "truncated": True},
        )

    def test_csv_budget_truncation_flags(self):
        csv_path = self.path("data.csv")
        _write_text(csv_path, "id,value\n" + "".join(f"{i},{i * 10}\n" for i in range(1, 6)))

        sheets = xlsx_reader.detect_and_load(csv_path, max_rows=3)

        self.assertEqual(len(sheets["data"]), 3)
        structure = xlsx_reader.explore_structure(sheets)
        self.assertEqual(
            structure["data"]["row_scope"],
            {"max_rows": 3, "loaded_rows": 4, "analyzed_rows": 3, "truncated": True},
        )
        self.assertEqual(structure["data"]["preview"][0], {"id": 1, "value": 10})

    def test_csv_short_table_within_budget(self):
        csv_path = self.path("short.csv")
        _write_text(csv_path, "id,value\n1,10\n2,20\n")

        sheets = xlsx_reader.detect_and_load(csv_path, max_rows=5)

        structure = xlsx_reader.explore_structure(sheets)
        self.assertEqual(
            structure["short"]["row_scope"],
            {"max_rows": 5, "loaded_rows": 2, "analyzed_rows": 2, "truncated": False},
        )

    def test_empty_and_exact_limit_tables(self):
        empty_path = self.path("empty.csv")
        _write_text(empty_path, "id,value\n")
        exact_path = self.path("exact.csv")
        _write_text(exact_path, "id,value\n1,10\n2,20\n3,30\n")

        empty_scope = xlsx_reader.explore_structure(
            xlsx_reader.detect_and_load(empty_path, max_rows=3)
        )["empty"]["row_scope"]
        self.assertEqual(empty_scope["analyzed_rows"], 0)
        self.assertFalse(empty_scope["truncated"])

        exact_scope = xlsx_reader.explore_structure(
            xlsx_reader.detect_and_load(exact_path, max_rows=3)
        )["exact"]["row_scope"]
        self.assertEqual(
            exact_scope,
            {"max_rows": 3, "loaded_rows": 3, "analyzed_rows": 3, "truncated": False},
        )

    def test_multiline_quoted_records_count_as_one_row(self):
        csv_path = self.path("quoted.csv")
        _write_text(
            csv_path,
            'id,note\n'
            '1,"line one\nstill one record"\n'
            '2,"two\nlines"\n'
            '3,plain\n'
            '4,tail\n'
            '5,tail\n',
        )

        sheets = xlsx_reader.detect_and_load(csv_path, max_rows=2)

        self.assertEqual(len(sheets["quoted"]), 2)
        structure = xlsx_reader.explore_structure(sheets)
        self.assertEqual(
            structure["quoted"]["row_scope"],
            {"max_rows": 2, "loaded_rows": 3, "analyzed_rows": 2, "truncated": True},
        )
        self.assertIn("\n", structure["quoted"]["preview"][1]["note"])

    def test_tsv_budget(self):
        tsv_path = self.path("tab.tsv")
        _write_text(tsv_path, "id\tvalue\n" + "".join(f"{i}\t{i * 10}\n" for i in range(1, 5)))

        sheets = xlsx_reader.detect_and_load(tsv_path, max_rows=2)

        self.assertEqual(len(sheets["tab"]), 2)
        structure = xlsx_reader.explore_structure(sheets)
        self.assertTrue(structure["tab"]["row_scope"]["truncated"])

    def test_encoding_metadata_retained_with_budget(self):
        csv_path = self.path("bom.csv")
        _write_text(csv_path, "id,value\n1,10\n2,20\n3,30\n", encoding="utf-8-sig")

        sheets = xlsx_reader.detect_and_load(csv_path, max_rows=2)

        self.assertEqual(sheets["bom"]._reader_encoding, "utf-8-sig")
        self.assertEqual(len(sheets["bom"]), 2)

    def test_pandas_io_receives_nplus_one(self):
        import pandas as pd

        csv_path = self.path("any.csv")
        _write_text(csv_path, "id\n1\n2\n3\n")
        df = pd.DataFrame({"id": [1, 2, 3]})
        with patch("pandas.read_excel", return_value={"Sheet1": df}) as mock_excel:
            xlsx_reader.detect_and_load(self.xlsx_path, max_rows=2)
        self.assertEqual(mock_excel.call_args.kwargs.get("nrows"), 3)
        self.assertEqual(mock_excel.call_args.kwargs.get("sheet_name"), None)

        with patch("pandas.read_csv", return_value=df) as mock_csv:
            xlsx_reader.detect_and_load(csv_path, max_rows=2)
        self.assertEqual(mock_csv.call_args.kwargs.get("nrows"), 3)

        with patch("pandas.read_excel", return_value={"Sheet1": df}) as mock_excel_named:
            xlsx_reader.detect_and_load(self.xlsx_path, sheet_name_filter="Sheet1", max_rows=4)
        self.assertEqual(mock_excel_named.call_args.kwargs.get("nrows"), 5)
        self.assertEqual(mock_excel_named.call_args.kwargs.get("sheet_name"), "Sheet1")

    def test_stats_and_nulls_scoped_to_analyzed_rows(self):
        csv_path = self.path("stats.csv")
        _write_text(csv_path, "id,value\n1,\n2,20\n3,30\n4,40\n5,50\n6,60\n")

        budgeted = xlsx_reader.detect_and_load(csv_path, max_rows=3)
        budgeted_structure = xlsx_reader.explore_structure(budgeted)
        budgeted_stats = xlsx_reader.compute_stats(budgeted)

        self.assertEqual(budgeted_structure["stats"]["null_columns"]["value"]["count"], 1)
        self.assertEqual(budgeted_stats["stats"]["value"]["mean"], 25.0)

        unrestricted = xlsx_reader.detect_and_load(csv_path)
        unrestricted_stats = xlsx_reader.compute_stats(unrestricted)
        self.assertEqual(unrestricted_stats["stats"]["value"]["mean"], 40.0)


class RowBudgetCliTests(_TempFileMixin, unittest.TestCase):
    def _run_cli(self, *args):
        return subprocess.run(
            [sys.executable, SCRIPT, *args], capture_output=True, text=True
        )

    def setUp(self):
        super().setUp()
        self.xlsx_path = self.path("book.xlsx")
        _write_workbook(
            self.xlsx_path,
            {
                "Sheet1": [["id", "value"]] + [[i, i * 10] for i in range(1, 6)],
                "Sheet2": [["id", "value"]] + [[i, i * 10] for i in range(1, 3)],
            },
        )

    def test_json_output_reports_row_scope(self):
        proc = self._run_cli(self.xlsx_path, "--json", "--max-rows", "2")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(
            payload["row_scope"],
            {
                "max_rows": 2,
                "analyzed_rows_total": 4,
                "loaded_rows_total": 5,
                "truncated_sheets": ["Sheet1"],
            },
        )
        self.assertEqual(
            payload["structure"]["Sheet1"]["row_scope"],
            {"max_rows": 2, "loaded_rows": 3, "analyzed_rows": 2, "truncated": True},
        )
        self.assertEqual(
            payload["structure"]["Sheet2"]["row_scope"],
            {"max_rows": 2, "loaded_rows": 2, "analyzed_rows": 2, "truncated": False},
        )

    def test_human_report_states_partial_scope(self):
        proc = self._run_cli(self.xlsx_path, "--max-rows", "2")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Row budget: analyzing at most 2 row(s) per sheet", proc.stdout)
        self.assertIn("Sheets truncated at budget: Sheet1", proc.stdout)
        self.assertIn("Row scope: analyzed 2 of 3 loaded row(s)", proc.stdout)
        self.assertIn(
            "Statistics and quality reflect analyzed rows only, not full-file totals",
            proc.stdout,
        )
        self.assertIn("Total analyzed rows across all sheets: 4", proc.stdout)

    def test_quality_only_mode_respects_budget(self):
        csv_path = self.path("dups.csv")
        _write_text(
            csv_path,
            "id,value\n1,10\n2,20\n3,30\n4,40\n1,10\n2,20\n",
        )

        budgeted = self._run_cli(csv_path, "--quality", "--json", "--max-rows", "4")
        unrestricted = self._run_cli(csv_path, "--quality", "--json")

        self.assertEqual(budgeted.returncode, 0, budgeted.stderr)
        self.assertEqual(unrestricted.returncode, 0, unrestricted.stderr)
        budgeted_types = [
            f["type"] for f in json.loads(budgeted.stdout)["quality"]["dups"]
        ]
        unrestricted_types = [
            f["type"] for f in json.loads(unrestricted.stdout)["quality"]["dups"]
        ]
        self.assertNotIn("duplicate_rows", budgeted_types)
        self.assertIn("duplicate_rows", unrestricted_types)
        self.assertEqual(
            json.loads(budgeted.stdout)["row_scope"]["analyzed_rows_total"], 4
        )

    def test_invalid_bounds_rejected_and_source_readonly(self):
        with open(self.xlsx_path, "rb") as f:
            before = f.read()

        for bad in ("0", "-3", "abc", "2.5"):
            proc = self._run_cli(self.xlsx_path, "--max-rows", bad)
            self.assertNotEqual(proc.returncode, 0, f"--max-rows {bad} should fail")
            self.assertIn("--max-rows", proc.stderr)

        with open(self.xlsx_path, "rb") as f:
            after = f.read()
        self.assertEqual(before, after)

    def test_default_unrestricted_control(self):
        json_proc = self._run_cli(self.xlsx_path, "--json")
        human_proc = self._run_cli(self.xlsx_path)

        self.assertEqual(json_proc.returncode, 0, json_proc.stderr)
        payload = json.loads(json_proc.stdout)
        self.assertNotIn("row_scope", payload)
        self.assertNotIn("row_scope", payload["structure"]["Sheet1"])
        self.assertEqual(payload["structure"]["Sheet1"]["shape"]["rows"], 5)
        self.assertEqual(human_proc.returncode, 0, human_proc.stderr)
        self.assertNotIn("Row budget", human_proc.stdout)
        self.assertIn("Total rows across all sheets: 7", human_proc.stdout)


if __name__ == "__main__":
    unittest.main()
