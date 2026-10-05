#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for formula_check.py worksheet-local defined-name visibility.

Regression tests for definedName localSheetId handling: formula_check.py
treated every defined name in workbook.xml as globally visible, so a
name scoped to one worksheet (LocalRate restricted to Sales) satisfied
formulas on every other sheet too — the Data formula referencing an
out-of-scope name must be reported as unknown.

localSheetId refers to the 0-based position of the owning worksheet in
the <sheets> element order — not to relationship IDs, numeric sheetId
attributes or worksheet filenames, all of which differ from the
ordinals in the fixtures below on purpose. Hidden sheets still occupy
positions, and --sheet filtering must not renumber them. Invalid or
out-of-range local scopes are never promoted to global visibility, and
a local name shadowing a global name stays valid on both worksheets.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(SCRIPTS_DIR, "formula_check.py")

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
{overrides}
</Types>
"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
"""


def worksheet_xml(formulas: dict[str, str]) -> str:
    """One <c> per {cell_ref: formula_text}, all in one row."""
    cells = "".join(
        f'<c r="{ref}"><f>{text}</f></c>' for ref, text in formulas.items()
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="{NS_MAIN}">
  <sheetData><row r="1">{cells}</row></sheetData>
</worksheet>
"""


def build_xlsx(path, sheets, defined_names_xml=""):
    """Pack a workbook. *sheets* is a list of dicts in <sheets> document
    order (that order defines the ordinals localSheetId refers to):
      name, sheetId (numeric attribute, deliberately unrelated),
      rid (relationship id, deliberately unrelated), target (worksheet
      part name, deliberately unrelated), formulas, hidden
    """
    sheet_tags = []
    overrides = []
    rels = []
    parts = {}
    for s in sheets:
        state = ' state="hidden"' if s.get("hidden") else ""
        sheet_tags.append(
            f'<sheet name="{s["name"]}" sheetId="{s["sheetId"]}"{state} r:id="{s["rid"]}"/>'
        )
        rels.append(
            f'<Relationship Id="{s["rid"]}"'
            ' Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"'
            f' Target="{s["target"].split("xl/", 1)[1]}"/>'
        )
        overrides.append(
            f'<Override PartName="/{s["target"]}"'
            ' ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
        parts[s["target"]] = worksheet_xml(s.get("formulas", {}))

    workbook = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{NS_MAIN}" xmlns:r="{NS_REL}">
  <sheets>
    {"".join(sheet_tags)}
  </sheets>
{defined_names_xml}
</workbook>
"""
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        + "\n".join(rels)
        + "\n</Relationships>\n"
    )

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES.format(overrides="\n".join(overrides)))
        zf.writestr("_rels/.rels", ROOT_RELS)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        for part, xml in parts.items():
            zf.writestr(part, xml)


def run_cli(xlsx_path, *extra):
    """Run the actual CLI; return (exit_code, parsed JSON results)."""
    proc = subprocess.run(
        [sys.executable, SCRIPT, xlsx_path, "--json", *extra],
        capture_output=True, text=True,
    )
    return proc.returncode, json.loads(proc.stdout)


def defined_names_block(*entries):
    """Build <definedNames> XML from (name, localSheetId-or-None) entries."""
    items = []
    for name, local in entries:
        attr = "" if local is None else f' localSheetId="{local}"'
        items.append(f'<definedName name="{name}"{attr}>$A$1</definedName>')
    if not items:
        return ""
    return f"  <definedNames>{''.join(items)}</definedNames>"


# Fixtures deliberately use sheetId attributes (7, 2), relationship ids
# (rId9, rId4) and part names (sheet3.xml, sheet1.xml) that do not match
# the <sheets> ordinals (0, 1) — confusing any of them for localSheetId
# breaks the assertions below.
SALES = dict(name="Sales", sheetId="7", rid="rId9", target="xl/worksheets/sheet3.xml")
DATA = dict(name="Data", sheetId="2", rid="rId4", target="xl/worksheets/sheet1.xml")


def base_workbook(sales_formulas, data_formulas, names_xml):
    sheets = [
        dict(SALES, formulas=sales_formulas),
        dict(DATA, formulas=data_formulas),
    ]
    return sheets, names_xml


class TestLocalDefinedNameVisibility(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "wb.xlsx")

    def _write(self, sheets, names_xml):
        build_xlsx(self.path, sheets, names_xml)
        return self.path

    def test_local_name_visible_only_on_owning_sheet(self):
        names = defined_names_block(("LocalRate", 0), ("GlobalRate", None))
        self._write(*base_workbook(
            {"A1": "LocalRate*2", "B1": "GlobalRate*2"},
            {"A1": "LocalRate*2", "B1": "GlobalRate*2"},
            names,
        ))
        code, out = run_cli(self.path)

        unknown = [e for e in out["errors"] if e["type"] == "unknown_name_ref"]
        self.assertEqual(
            [(e["sheet"], e["unknown_name"]) for e in unknown],
            [("Data", "LocalRate")],
            "LocalRate is scoped to Sales; the Data formula must flag it",
        )
        # The --json path exits 1 on any recorded error_count, including
        # heuristic-only unknown_name_ref findings (only the human-readable
        # output separates warnings from hard failures).
        self.assertEqual(code, 1)

    def test_diagnostics_list_only_names_visible_at_formula_location(self):
        names = defined_names_block(("LocalRate", 0), ("GlobalRate", None))
        self._write(*base_workbook({"A1": "LocalRate*2"}, {"A1": "LocalRate*2"}, names))
        _, out = run_cli(self.path)

        unknown = [e for e in out["errors"] if e["type"] == "unknown_name_ref"]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(
            unknown[0]["defined_names"], ["GlobalRate"],
            "the Data diagnostic must not advertise Sales-local names",
        )

    def test_hidden_sheet_still_occupies_ordinal_position(self):
        # Ordinals: Hidden=0, Sales=1, Data=2. LocalRate is scoped to
        # Sales via localSheetId=1 even though a hidden sheet precedes it.
        names = defined_names_block(("LocalRate", 1))
        sheets = [
            dict(SALES, name="Hidden", sheetId="9", rid="rId1",
                 target="xl/worksheets/sheet9.xml", hidden=True, formulas={}),
            dict(SALES, formulas={"A1": "LocalRate*2"}),
            dict(DATA, formulas={"A1": "LocalRate*2"}),
        ]
        self._write(sheets, names)
        _, out = run_cli(self.path)

        unknown = [e for e in out["errors"] if e["type"] == "unknown_name_ref"]
        self.assertEqual(
            [(e["sheet"], e["unknown_name"]) for e in unknown],
            [("Data", "LocalRate")],
        )

    def test_sheet_filter_keeps_original_ordinals(self):
        # Filtering to Data must not renumber sheets: LocalRate scoped to
        # Data itself (ordinal 1) resolves, the Sales-local name does not.
        names = defined_names_block(("LocalRate", 0), ("DataRate", 1))
        self._write(*base_workbook(
            {"A1": "DataRate*2"},
            {"A1": "LocalRate*2", "B1": "DataRate*2"},
            names,
        ))
        _, out = run_cli(self.path, "--sheet", "Data")

        self.assertEqual(out["sheets_checked"], ["Data"])
        unknown = [e for e in out["errors"] if e["type"] == "unknown_name_ref"]
        self.assertEqual(
            [(e["sheet"], e["unknown_name"]) for e in unknown],
            [("Data", "LocalRate")],
        )

    def test_out_of_range_local_scope_not_promoted_to_global(self):
        # Only two sheets (ordinals 0, 1): localSheetId=5 is out of range
        # and must make the name invisible everywhere, not global.
        names = defined_names_block(("GhostRate", 5))
        self._write(*base_workbook({"A1": "GhostRate*2"}, {"A1": "GhostRate*2"}, names))
        _, out = run_cli(self.path)

        unknown = [e for e in out["errors"] if e["type"] == "unknown_name_ref"]
        self.assertEqual(
            [(e["sheet"], e["unknown_name"]) for e in unknown],
            [("Sales", "GhostRate"), ("Data", "GhostRate")],
        )

    def test_malformed_local_scope_not_promoted_to_global(self):
        names = defined_names_block(("BadRate", "rId9"))
        self._write(*base_workbook({"A1": "BadRate*2"}, {"A1": "BadRate*2"}, names))
        _, out = run_cli(self.path)

        unknown = [e for e in out["errors"] if e["type"] == "unknown_name_ref"]
        self.assertEqual(len(unknown), 2)
        for e in unknown:
            self.assertEqual(e["defined_names"], [])

    def test_local_name_shadowing_global_stays_valid_on_both_sheets(self):
        names = defined_names_block(
            ("SharedRate", None), ("SharedRate", 1), ("GlobalRate", None),
        )
        self._write(*base_workbook(
            {"A1": "SharedRate*2", "B1": "GlobalRate*2"},
            {"A1": "SharedRate*2", "B1": "GlobalRate*2"},
            names,
        ))
        _, out = run_cli(self.path)

        unknown = [e for e in out["errors"] if e["type"] == "unknown_name_ref"]
        self.assertEqual(unknown, [], "shadowed names are valid on both sheets")

    def test_source_bytes_preserved_across_run(self):
        names = defined_names_block(("LocalRate", 0))
        self._write(*base_workbook({"A1": "LocalRate*2"}, {"A1": "LocalRate*2"}, names))
        before = open(self.path, "rb").read()
        run_cli(self.path)
        after = open(self.path, "rb").read()
        self.assertEqual(before, after, "formula_check.py must not modify the workbook")


if __name__ == "__main__":
    unittest.main()
