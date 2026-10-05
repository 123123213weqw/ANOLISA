#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for formula_check.py case-insensitive reference identity.

Regression tests for issue #5556: worksheet and defined-name references
resolve case-insensitively in Excel, but check() compared the spellings
extracted from formulas directly against workbook.xml declarations, so a
workbook containing Sales and Interest_Rate was falsely rejected for
sales!A1 or INTEREST_RATE*2.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(SCRIPTS_DIR, "formula_check.py")

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '</Types>'
)

RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<Relationships xmlns="{PKG_REL}">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
    '</Relationships>'
)


def build_xlsx(path: str, formulas: list[str],
               sheet1: str = "Sales", sheet2: str = "Data",
               defined_name: str = "Interest_Rate") -> str:
    """Pack a real workbook: two named sheets, one defined name, formulas."""
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{NS}" xmlns:r="{REL}"><sheets>'
        f'<sheet name="{sheet1}" sheetId="1" r:id="rId1"/>'
        f'<sheet name="{sheet2}" sheetId="2" r:id="rId2"/>'
        '</sheets><definedNames>'
        f'<definedName name="{defined_name}">{sheet1}!$A$1</definedName>'
        '</definedNames></workbook>'
    )
    rows = "".join(
        f'<row r="{i + 1}"><c r="A{i + 1}"><f>{f}</f><v>1</v></c></row>'
        for i, f in enumerate(formulas)
    )
    sheet1_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{NS}"><sheetData>{rows}</sheetData></worksheet>'
    )
    sheet2_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{NS}"><sheetData>'
        '<row r="1"><c r="A1"><v>1</v></c></row>'
        '</sheetData></worksheet>'
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", RELS)
        z.writestr("xl/worksheets/sheet1.xml", sheet1_xml)
        z.writestr("xl/worksheets/sheet2.xml", sheet2_xml)
    return path


def run_check(path: str) -> tuple[int, dict]:
    result = subprocess.run([sys.executable, SCRIPT, path, "--json"],
                            capture_output=True, text=True)
    return result.returncode, json.loads(result.stdout)


class TestFormulaCheckCaseInsensitiveRefs(unittest.TestCase):
    def test_mixed_case_unquoted_sheet_ref_is_accepted(self):
        """`sales!A1` must resolve against a sheet named Sales (issue #5556)."""
        with tempfile.TemporaryDirectory() as root:
            x = build_xlsx(os.path.join(root, "case.xlsx"), ["sales!A1+1"])
            code, out = run_check(x)
            self.assertEqual((code, out["error_count"]), (0, 0), out["errors"])

    def test_mixed_case_quoted_sheet_ref_is_accepted(self):
        """`'SALES'!A1` must resolve against a sheet named Sales."""
        with tempfile.TemporaryDirectory() as root:
            x = build_xlsx(os.path.join(root, "case.xlsx"), ["'SALES'!A1+1"])
            code, out = run_check(x)
            self.assertEqual((code, out["error_count"]), (0, 0), out["errors"])

    def test_non_ascii_quoted_sheet_ref_case_is_ignored(self):
        """A non-ASCII sheet name referenced in a different case resolves."""
        with tempfile.TemporaryDirectory() as root:
            x = build_xlsx(os.path.join(root, "case.xlsx"), ["'ärger'!A1+1"],
                           sheet1="Ärger", sheet2="Data")
            code, out = run_check(x)
            self.assertEqual((code, out["error_count"]), (0, 0), out["errors"])

    def test_mixed_case_defined_name_is_accepted(self):
        """`INTEREST_RATE*2` must resolve against Interest_Rate (issue #5556)."""
        with tempfile.TemporaryDirectory() as root:
            x = build_xlsx(os.path.join(root, "case.xlsx"), ["INTEREST_RATE*2"])
            code, out = run_check(x)
            self.assertEqual((code, out["error_count"]), (0, 0), out["errors"])

    def test_exact_case_references_still_pass(self):
        """Control: exact-case sheet and name references keep passing."""
        with tempfile.TemporaryDirectory() as root:
            x = build_xlsx(os.path.join(root, "case.xlsx"),
                           ["Data!A1+Interest_Rate"])
            code, out = run_check(x)
            self.assertEqual((code, out["error_count"]), (0, 0), out["errors"])

    def test_genuinely_missing_sheet_is_reported_with_original_spelling(self):
        """Control: a real missing sheet is still a broken_sheet_ref error."""
        with tempfile.TemporaryDirectory() as root:
            x = build_xlsx(os.path.join(root, "case.xlsx"), ["Nope!A1+1"])
            code, out = run_check(x)
            self.assertEqual(code, 1)
            self.assertEqual(out["error_count"], 1)
            error = out["errors"][0]
            self.assertEqual(error["type"], "broken_sheet_ref")
            self.assertEqual(error["missing_sheet"], "Nope")
            self.assertEqual(error["valid_sheets"], ["Data", "Sales"])

    def test_genuinely_missing_name_is_reported_with_original_spelling(self):
        """Control: a real unknown name is still an unknown_name_ref error."""
        with tempfile.TemporaryDirectory() as root:
            x = build_xlsx(os.path.join(root, "case.xlsx"), ["Not_Defined+1"])
            code, out = run_check(x)
            self.assertEqual(out["error_count"], 1)
            error = out["errors"][0]
            self.assertEqual(error["type"], "unknown_name_ref")
            self.assertEqual(error["unknown_name"], "Not_Defined")
            self.assertEqual(error["defined_names"], ["Interest_Rate"])


if __name__ == "__main__":
    unittest.main()
