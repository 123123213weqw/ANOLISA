#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for xlsx_pack.py atomic publication.

Regression tests for packing failures midway through writing archive
members (issue #5554): xlsx_pack used to open the destination in "w"
mode before copying members, so a member read/write failure destroyed a
previously packed workbook at the destination and left a partial archive
behind — including when no output previously existed. The tests inject a
real I/O failure (an unreadable non-XML member) into a real source tree
and a real previous ZIP archive.
"""

import os
import subprocess
import sys
import tempfile
import unittest
import zipfile

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(SCRIPTS_DIR, "xlsx_pack.py")

CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '</Types>'
)

WORKBOOK_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>'
)


def build_source(root: str) -> str:
    """Materialize a minimal valid unpacked package in <root>/src."""
    src = os.path.join(root, "src")
    os.mkdir(src)
    with open(os.path.join(src, "[Content_Types].xml"), "w") as f:
        f.write(CONTENT_TYPES_XML)
    os.makedirs(os.path.join(src, "xl"))
    with open(os.path.join(src, "xl", "workbook.xml"), "w") as f:
        f.write(WORKBOOK_XML)
    return src


def add_locked_member(src: str) -> str:
    """Add a non-XML member that passes XML preflight but fails z.write.

    The .bin extension keeps xml validation away from it; mode 0 makes
    reading it fail with PermissionError once archive writing reaches it.
    """
    locked = os.path.join(src, "zz_locked.bin")
    with open(locked, "wb") as f:
        f.write(b"\x00" * 16)
    os.chmod(locked, 0)
    return locked


def build_previous_output(path: str) -> bytes:
    """Write a real previous workbook and return its exact bytes."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("marker.txt", "PREVIOUS-VALID-WORKBOOK")
    with open(path, "rb") as f:
        return f.read()


def unlock(path: str) -> None:
    """Restore a locked member's mode once its tree is gone."""
    try:
        os.chmod(path, 0o644)
    except FileNotFoundError:
        pass


def run_pack(src: str, out: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, SCRIPT, src, out],
                          capture_output=True, text=True)


class TestXlsxPackAtomicOutput(unittest.TestCase):
    def test_member_write_failure_preserves_previous_output(self):
        """A failure midway must leave the previous workbook untouched (issue #5554)."""
        with tempfile.TemporaryDirectory() as root:
            src = build_source(root)
            locked = add_locked_member(src)
            self.addCleanup(unlock, locked)
            out = os.path.join(root, "out.xlsx")
            previous = build_previous_output(out)

            result = run_pack(src, out)
            self.assertNotEqual(result.returncode, 0, result.stdout)

            with open(out, "rb") as f:
                self.assertEqual(f.read(), previous)
            with zipfile.ZipFile(out) as z:
                self.assertEqual(z.namelist(), ["marker.txt"])

    def test_member_write_failure_leaves_no_partial_output(self):
        """With no previous output, a failure must not publish an archive (issue #5554)."""
        with tempfile.TemporaryDirectory() as root:
            src = build_source(root)
            locked = add_locked_member(src)
            self.addCleanup(unlock, locked)
            out = os.path.join(root, "fresh.xlsx")

            result = run_pack(src, out)
            self.assertNotEqual(result.returncode, 0, result.stdout)

            self.assertFalse(os.path.exists(out))
            leftovers = [f for f in os.listdir(root) if f.startswith("fresh")]
            self.assertEqual(leftovers, [])

    def test_successful_pack_replaces_output_completely(self):
        """Control: a successful pack publishes the full member set."""
        with tempfile.TemporaryDirectory() as root:
            src = build_source(root)
            out = os.path.join(root, "out.xlsx")
            build_previous_output(out)

            result = run_pack(src, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Packed 2 files", result.stdout)

            with zipfile.ZipFile(out) as z:
                self.assertEqual(
                    sorted(z.namelist()),
                    ["[Content_Types].xml", "xl/workbook.xml"],
                )
            leftovers = [f for f in os.listdir(root) if f.endswith(".tmp")]
            self.assertEqual(leftovers, [])

    def test_successful_pack_preserves_output_permissions(self):
        """A repack keeps the permissions of the existing output file."""
        with tempfile.TemporaryDirectory() as root:
            src = build_source(root)
            out = os.path.join(root, "out.xlsx")
            build_previous_output(out)
            os.chmod(out, 0o640)

            result = run_pack(src, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(os.stat(out).st_mode & 0o7777, 0o640)

    def test_output_symlink_writes_through_to_target(self):
        """Control: packing to a symlinked path updates the target, keeps the link."""
        with tempfile.TemporaryDirectory() as root:
            src = build_source(root)
            target = os.path.join(root, "real.xlsx")
            build_previous_output(target)
            link = os.path.join(root, "link.xlsx")
            os.symlink(target, link)

            result = run_pack(src, link)
            self.assertEqual(result.returncode, 0, result.stderr)

            self.assertTrue(os.path.islink(link))
            with zipfile.ZipFile(target) as z:
                self.assertEqual(
                    sorted(z.namelist()),
                    ["[Content_Types].xml", "xl/workbook.xml"],
                )

    def test_xml_validation_failure_keeps_previous_output(self):
        """Control: a malformed XML source exits 1 before touching the output."""
        with tempfile.TemporaryDirectory() as root:
            src = build_source(root)
            with open(os.path.join(src, "xl", "workbook.xml"), "w") as f:
                f.write("<workbook>not closed")
            out = os.path.join(root, "out.xlsx")
            previous = build_previous_output(out)

            result = run_pack(src, out)
            self.assertEqual(result.returncode, 1)
            self.assertIn("XML parse errors", result.stderr)

            with open(out, "rb") as f:
                self.assertEqual(f.read(), previous)


if __name__ == "__main__":
    unittest.main()
