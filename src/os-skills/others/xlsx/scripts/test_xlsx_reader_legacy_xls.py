#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Regression tests for the optional legacy XLS backend in xlsx_reader.py.

The reader used to reject every .xls workbook outright, forcing users to
convert local BIFF files before readonly analysis even though pandas can
read them through the xlrd engine. These tests build real .xls workbooks
with xlwt (multiple and Unicode worksheet names, quoted CJK labels,
numbers, formatted dates) and run both the loader and the actual CLI,
plus invalid-binary, unknown-sheet, missing-backend and modern-Excel
paths. No network and no Excel installation is involved.
"""

import datetime
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import xlwt
    from xlwt import XFStyle
except ImportError:  # fixture builder is itself optional
    xlwt = None

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(SCRIPTS_DIR, "xlsx_reader.py")


def _xlrd_available() -> bool:
    try:
        import xlrd  # noqa: F401
        return True
    except ImportError:
        return False


def _load_reader():
    spec = importlib.util.spec_from_file_location("xlsx_reader_xls_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build_workbook(path: str) -> None:
    """Two worksheets (ASCII + Unicode name), CJK labels, numbers, a date."""
    wb = xlwt.Workbook()
    alpha = wb.add_sheet("Alpha")
    alpha.write(0, 0, "city")
    alpha.write(0, 1, "temp")
    alpha.write(1, 0, "oslo")
    alpha.write(1, 1, 21.5)
    alpha.write(2, 0, "stockholm")
    alpha.write(2, 1, 19.0)

    second = wb.add_sheet("第二")
    second.write(0, 0, '标签,带逗号')
    second.write(0, 1, '她说："好"')
    second.write(1, 0, 42)
    date_style = XFStyle()
    date_style.num_format_str = "M/D/YY"
    second.write(1, 1, datetime.datetime(2024, 3, 14), date_style)
    wb.save(path)


def _run_cli(*args: str, timeout: int = 120):
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        capture_output=True, text=True, timeout=timeout,
    )


class LegacyXlsBackendTests(unittest.TestCase):
    """Legacy .xls workbooks analyze through the optional xlrd backend."""

    @classmethod
    def setUpClass(cls):
        if xlwt is None:
            raise unittest.SkipTest("xlwt not installed; cannot build fixtures")
        if not _xlrd_available():
            raise unittest.SkipTest("optional xlrd backend not installed")

    def test_all_worksheets_loaded_in_workbook_order(self):
        reader = _load_reader()
        with tempfile.TemporaryDirectory() as root:
            book = os.path.join(root, "legacy.xls")
            _build_workbook(book)
            sheets = reader.detect_and_load(book)
            self.assertEqual(list(sheets.keys()), ["Alpha", "第二"])
            self.assertEqual(sheets["Alpha"].iloc[0]["city"], "oslo")

    def test_named_unicode_sheet_selection(self):
        reader = _load_reader()
        with tempfile.TemporaryDirectory() as root:
            book = os.path.join(root, "legacy.xls")
            _build_workbook(book)
            sheets = reader.detect_and_load(book, sheet_name_filter="第二")
            self.assertEqual(list(sheets.keys()), ["第二"])
            self.assertEqual(len(sheets["第二"]), 1)

    def test_numbers_dates_and_cjk_labels_preserved(self):
        reader = _load_reader()
        with tempfile.TemporaryDirectory() as root:
            book = os.path.join(root, "legacy.xls")
            _build_workbook(book)
            df = reader.detect_and_load(book)["第二"]
            self.assertEqual(
                list(df.columns), ['标签,带逗号', '她说："好"']
            )
            self.assertEqual(df.iloc[0]["标签,带逗号"], 42)
            self.assertEqual(str(df.iloc[0]["她说：\"好\""]), "2024-03-14 00:00:00")
            alpha = reader.detect_and_load(book)["Alpha"]
            self.assertEqual(alpha["temp"].dtype.kind, "f")
            self.assertEqual(alpha.iloc[0]["temp"], 21.5)

    def test_source_bytes_unchanged_after_load_and_cli(self):
        reader = _load_reader()
        with tempfile.TemporaryDirectory() as root:
            book = os.path.join(root, "legacy.xls")
            _build_workbook(book)
            before = hashlib.md5(Path(book).read_bytes()).hexdigest()
            reader.detect_and_load(book)
            result = _run_cli(book, "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            after = hashlib.md5(Path(book).read_bytes()).hexdigest()
            self.assertEqual(before, after)

    def test_cli_json_report_lists_worksheets(self):
        with tempfile.TemporaryDirectory() as root:
            book = os.path.join(root, "legacy.xls")
            _build_workbook(book)
            result = _run_cli(book, "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(
                sorted(report["structure"].keys()), ["Alpha", "第二"]
            )
            row = report["structure"]["Alpha"]["preview"][0]
            self.assertEqual(row["city"], "oslo")

    def test_invalid_binary_is_ordinary_read_error(self):
        reader = _load_reader()
        with tempfile.TemporaryDirectory() as root:
            book = os.path.join(root, "broken.xls")
            Path(book).write_bytes(b"garbage bytes, definitely not a BIFF stream")
            with self.assertRaises(ValueError) as ctx:
                reader.detect_and_load(book)
            self.assertIn(book, str(ctx.exception))

            result = _run_cli(book)
            self.assertEqual(result.returncode, 1)
            self.assertIn("ERROR:", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_unknown_sheet_is_actionable_cli_error(self):
        with tempfile.TemporaryDirectory() as root:
            book = os.path.join(root, "legacy.xls")
            _build_workbook(book)
            result = _run_cli(book, "--sheet", "Ghost")
            self.assertEqual(result.returncode, 1)
            self.assertIn("Ghost", result.stderr)
            self.assertIn("ERROR:", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

class MissingBackendTests(unittest.TestCase):
    """Without the optional backend the failure must be actionable."""

    @classmethod
    def setUpClass(cls):
        if xlwt is None:
            raise unittest.SkipTest("xlwt not installed; cannot build fixtures")
        if _xlrd_available():
            raise unittest.SkipTest("xlrd installed; missing-backend path skipped")

    def test_missing_backend_actionable_install_error(self):
        reader = _load_reader()
        with tempfile.TemporaryDirectory() as root:
            book = os.path.join(root, "legacy.xls")
            _build_workbook(book)
            with self.assertRaises(RuntimeError) as ctx:
                reader.detect_and_load(book)
            message = str(ctx.exception)
            self.assertIn("xlrd", message)
            self.assertIn("pip install", message)

class ModernExcelControlTests(unittest.TestCase):
    """Modern OOXML input stays independent of the legacy backend."""

    def test_modern_xlsx_independent_of_legacy_backend(self):
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("openpyxl not installed")

        reader = _load_reader()
        with tempfile.TemporaryDirectory() as root:
            book = os.path.join(root, "modern.xlsx")
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = "S1"
            ws.append(["m", "n"])
            ws.append([3, 4])
            wb.save(book)
            sheets = reader.detect_and_load(book)
            self.assertEqual(list(sheets.keys()), ["S1"])
            self.assertEqual(sheets["S1"].iloc[0]["n"], 4)


if __name__ == "__main__":
    unittest.main()
