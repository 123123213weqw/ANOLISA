#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for formula_check.py --report publication safety.

Regression tests for the report output path: writing the JSON report must
never destroy the input workbook (identical path, hard-link or symlink
alias), never leave a partial report behind when a write/replace fails,
never lose the previous report, and keep ordinary report output (schema,
stdout mode, destination permissions, non-input symlink targets) intact.
"""

import builtins
import importlib.util
import io
import contextlib
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(SCRIPTS_DIR, "formula_check.py")

IS_WINDOWS = sys.platform.startswith("win")

WORKBOOK_XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{NS_MAIN}" xmlns:r="{NS_REL}">
  <sheets>
    <sheet name="Sheet1" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>
"""

RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
    Target="worksheets/sheet1.xml"/>
</Relationships>
"""

SHEET_XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="{NS_MAIN}">
  <sheetData>
    <row r="1"><c r="A1"><f>SUM(B1:B2)</f></c></row>
  </sheetData>
</worksheet>
"""

PREVIOUS_REPORT = '{\n  "status": "previous report",\n  "total_errors": 7\n}\n'


def build_xlsx(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", WORKBOOK_XML)
        z.writestr("xl/_rels/workbook.xml.rels", RELS_XML)
        z.writestr("xl/worksheets/sheet1.xml", SHEET_XML)
    with open(path, "rb") as f:
        return f.read()


def run_check(xlsx_path, *extra):
    return subprocess.run(
        [sys.executable, SCRIPT, xlsx_path, *extra],
        capture_output=True,
        text=True,
        timeout=60,
    )


def load_module():
    spec = importlib.util.spec_from_file_location("formula_check_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def staging_leftovers(directory):
    return [n for n in os.listdir(directory) if n.endswith(".tmp") or n.startswith(".")]


class TestInputAliasesRefused(unittest.TestCase):
    def test_report_path_equal_to_input_refused(self):
        """-o <input.xlsx> must not replace the workbook bytes with JSON."""
        with tempfile.TemporaryDirectory() as root:
            xlsx = os.path.join(root, "book.xlsx")
            original = build_xlsx(xlsx)

            result = run_check(xlsx, "--report", "-o", xlsx)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Error", result.stderr)
            self.assertIn("book.xlsx", result.stderr)
            with open(xlsx, "rb") as f:
                self.assertEqual(f.read(), original, "workbook bytes were destroyed")

    @unittest.skipIf(IS_WINDOWS, "hard links need POSIX semantics")
    def test_report_hardlink_alias_refused(self):
        """A hard-link alias of the input workbook must not be overwritten."""
        with tempfile.TemporaryDirectory() as root:
            xlsx = os.path.join(root, "book.xlsx")
            original = build_xlsx(xlsx)
            alias = os.path.join(root, "report.json")
            os.link(xlsx, alias)

            result = run_check(xlsx, "--report", "-o", alias)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Error", result.stderr)
            with open(alias, "rb") as f:
                self.assertEqual(f.read(), original, "workbook alias bytes were destroyed")

    @unittest.skipIf(IS_WINDOWS, "symlinks need POSIX semantics")
    def test_report_symlink_alias_refused(self):
        """A symlink pointing at the input workbook must not be followed."""
        with tempfile.TemporaryDirectory() as root:
            xlsx = os.path.join(root, "book.xlsx")
            original = build_xlsx(xlsx)
            alias = os.path.join(root, "report.json")
            os.symlink(xlsx, alias)

            result = run_check(xlsx, "--report", "-o", alias)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Error", result.stderr)
            self.assertTrue(os.path.islink(alias), "symlink was replaced")
            with open(xlsx, "rb") as f:
                self.assertEqual(f.read(), original, "workbook bytes were destroyed")


class TestWriteFailurePreservesPreviousReport(unittest.TestCase):
    def _run_main(self, mod, argv, stderr_buf):
        with mock.patch.object(sys, "argv", argv):
            with contextlib.redirect_stderr(stderr_buf):
                mod.main()

    def test_partial_write_preserves_previous_report(self):
        """A write that really writes a prefix then fails must keep the old report."""
        mod = load_module()
        with tempfile.TemporaryDirectory() as root:
            xlsx = os.path.join(root, "book.xlsx")
            build_xlsx(xlsx)
            dest = os.path.join(root, "report.json")
            with open(dest, "w", encoding="utf-8") as f:
                f.write(PREVIOUS_REPORT)

            real_fdopen = os.fdopen
            real_open = builtins.open
            dest_real = os.path.realpath(dest)

            def exploding_fdopen(fd, *args, **kwargs):
                handle = real_fdopen(fd, *args, **kwargs)
                orig_write = handle.write

                def write_then_fail(data):
                    orig_write(data[: max(1, len(data) // 2)])
                    raise OSError("injected partial write failure")

                handle.write = write_then_fail
                return handle

            def exploding_open(file, mode="r", *args, **kwargs):
                handle = real_open(file, mode, *args, **kwargs)
                if "w" in mode and os.path.realpath(file) == dest_real:
                    orig_write = handle.write

                    def write_then_fail(data):
                        orig_write(data[: max(1, len(data) // 2)])
                        raise OSError("injected partial write failure")

                    handle.write = write_then_fail
                return handle

            stderr_buf = io.StringIO()
            with mock.patch("os.fdopen", exploding_fdopen), \
                    mock.patch("builtins.open", exploding_open):
                with self.assertRaises(SystemExit) as ctx:
                    self._run_main(mod, ["formula_check.py", xlsx, "--report", "-o", dest],
                                   stderr_buf)

            self.assertNotEqual(ctx.exception.code, 0)
            self.assertIn("Error", stderr_buf.getvalue())
            with open(dest, encoding="utf-8") as f:
                self.assertEqual(f.read(), PREVIOUS_REPORT,
                                 "previous report was lost or partially replaced")
            self.assertEqual(staging_leftovers(root), [],
                             "partial staging report was left behind")

    def test_replace_failure_preserves_previous_report(self):
        """A failing final replace must keep the old report and clean staging."""
        mod = load_module()
        with tempfile.TemporaryDirectory() as root:
            xlsx = os.path.join(root, "book.xlsx")
            build_xlsx(xlsx)
            dest = os.path.join(root, "report.json")
            with open(dest, "w", encoding="utf-8") as f:
                f.write(PREVIOUS_REPORT)

            def exploding_replace(src, dst):
                raise OSError("injected replace failure")

            stderr_buf = io.StringIO()
            with mock.patch("os.replace", exploding_replace):
                with self.assertRaises(SystemExit) as ctx:
                    self._run_main(mod, ["formula_check.py", xlsx, "--report", "-o", dest],
                                   stderr_buf)

            self.assertNotEqual(ctx.exception.code, 0)
            self.assertIn("Error", stderr_buf.getvalue())
            with open(dest, encoding="utf-8") as f:
                self.assertEqual(f.read(), PREVIOUS_REPORT)
            self.assertEqual(staging_leftovers(root), [],
                             "staging report was left behind")

    def test_missing_parent_reports_error_without_traceback(self):
        """A missing parent directory keeps the nonzero error behavior, cleanly."""
        with tempfile.TemporaryDirectory() as root:
            xlsx = os.path.join(root, "book.xlsx")
            build_xlsx(xlsx)
            dest = os.path.join(root, "missing", "report.json")

            result = run_check(xlsx, "--report", "-o", dest)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Error", result.stderr)
            self.assertNotIn("Traceback", result.stderr)


class TestOrdinaryPublicationPreserved(unittest.TestCase):
    def test_ordinary_report_control(self):
        """A normal -o publication keeps schema, trailing newline and exit code."""
        import json

        with tempfile.TemporaryDirectory() as root:
            xlsx = os.path.join(root, "book.xlsx")
            build_xlsx(xlsx)
            dest = os.path.join(root, "report.json")

            result = run_check(xlsx, "--report", "-o", dest)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "", "-o must not print the report to stdout")
            with open(dest, encoding="utf-8") as f:
                content = f.read()
            self.assertTrue(content.endswith("\n"))
            report = json.loads(content)
            self.assertEqual(report["status"], "success")
            self.assertEqual(report["total_errors"], 0)
            self.assertEqual(report["file"], xlsx)

            stdout_result = run_check(xlsx, "--report")
            self.assertEqual(stdout_result.returncode, 0)
            self.assertEqual(stdout_result.stdout.strip(), content.strip(),
                             "stdout mode and file mode must agree")

    @unittest.skipIf(IS_WINDOWS, "mode bits need POSIX semantics")
    def test_destination_mode_retained(self):
        """An existing destination keeps its permission bits."""
        with tempfile.TemporaryDirectory() as root:
            xlsx = os.path.join(root, "book.xlsx")
            build_xlsx(xlsx)
            dest = os.path.join(root, "report.json")
            with open(dest, "w", encoding="utf-8") as f:
                f.write(PREVIOUS_REPORT)
            os.chmod(dest, 0o640)

            result = run_check(xlsx, "--report", "-o", dest)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(os.stat(dest).st_mode & 0o777, 0o640)

    @unittest.skipIf(IS_WINDOWS, "symlinks need POSIX semantics")
    def test_non_input_symlink_target_written_through(self):
        """A destination symlink to another file keeps pointing at its target."""
        import json

        with tempfile.TemporaryDirectory() as root:
            xlsx = os.path.join(root, "book.xlsx")
            build_xlsx(xlsx)
            target = os.path.join(root, "real-report.json")
            with open(target, "w", encoding="utf-8") as f:
                f.write(PREVIOUS_REPORT)
            dest = os.path.join(root, "link-report.json")
            os.symlink(target, dest)

            result = run_check(xlsx, "--report", "-o", dest)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(os.path.islink(dest), "destination symlink was replaced")
            with open(dest, encoding="utf-8") as f:
                report = json.load(f)
            self.assertEqual(report["status"], "success")
            self.assertEqual(staging_leftovers(root), [])


if __name__ == "__main__":
    unittest.main()
