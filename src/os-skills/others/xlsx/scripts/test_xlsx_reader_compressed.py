#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Regression tests for compressed CSV/TSV input in xlsx_reader.py.

The reader used to look at only the final path suffix, so .csv.gz/.tsv.gz
and the bzip2/xz equivalents were rejected as unsupported even though
pandas reads those standard single-stream codecs natively. These tests
cover the complete six-format family against equivalent plain DataFrames,
uppercase/multi-dot names, encoding fallback inside compression, header-only
inputs, quoted/Unicode fields, source preservation, corrupted streams, CLI
paths, and unchanged plain/Excel behavior.

Everything runs against real files and the real CLI in a temp directory;
no network and no Excel installation is involved.
"""

import bz2
import gzip
import hashlib
import importlib.util
import json
import lzma
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(SCRIPTS_DIR, "xlsx_reader.py")

_COMPRESSORS = {".gz": gzip.compress, ".bz2": bz2.compress, ".xz": lzma.compress}


def _load_reader():
    spec = importlib.util.spec_from_file_location("xlsx_reader_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_compressed(path: str, data: bytes) -> None:
    ext = Path(path).suffix.lower()
    with open(path, "wb") as fh:
        fh.write(_COMPRESSORS[ext](data))


class CompressedTableInputTests(unittest.TestCase):
    """Compressed CSV/TSV datasets load like their plain equivalents."""

    def test_six_format_family_matches_plain_equivalents(self):
        reader = _load_reader()
        content = "city,temp\noslo,21\nstockholm,19\n"
        for table in (".csv", ".tsv"):
            payload = content.replace(",", "\t") if table == ".tsv" else content
            for codec in (".gz", ".bz2", ".xz"):
                with tempfile.TemporaryDirectory() as root:
                    plain = os.path.join(root, f"data{table}")
                    packed = os.path.join(root, f"data{table}{codec}")
                    Path(plain).write_bytes(payload.encode("utf-8"))
                    _write_compressed(packed, payload.encode("utf-8"))
                    sheets = reader.detect_and_load(packed)
                    self.assertEqual(list(sheets.keys()), ["data"], packed)
                    expected = reader.detect_and_load(plain)["data"]
                    self.assertTrue(sheets["data"].equals(expected), packed)

    def test_uppercase_extensions_load_via_explicit_codec(self):
        reader = _load_reader()
        with tempfile.TemporaryDirectory() as root:
            packed = os.path.join(root, "UPPER.CSV.GZ")
            _write_compressed(packed, b"a,b\n1,2\n")
            sheets = reader.detect_and_load(packed)
            self.assertEqual(list(sheets["UPPER"].columns), ["a", "b"])

    def test_multi_dot_name_keeps_table_stem_and_delimiter(self):
        reader = _load_reader()
        with tempfile.TemporaryDirectory() as root:
            packed = os.path.join(root, "report.final.tsv.xz")
            _write_compressed(packed, b"key\tvalue\nk1\tv1\n")
            sheets = reader.detect_and_load(packed)
            self.assertEqual(list(sheets.keys()), ["report.final"])
            self.assertEqual(list(sheets["report.final"].columns), ["key", "value"])
            self.assertEqual(sheets["report.final"].iloc[0]["value"], "v1")

    def test_gbk_content_inside_gzip_uses_encoding_fallback(self):
        reader = _load_reader()
        payload = "城市,温度\n奥斯陆,21\n".encode("gbk")
        with tempfile.TemporaryDirectory() as root:
            packed = os.path.join(root, "cn.csv.gz")
            _write_compressed(packed, payload)
            df = reader.detect_and_load(packed)["cn"]
            self.assertEqual(list(df.columns), ["城市", "温度"])
            self.assertEqual(df.iloc[0]["城市"], "奥斯陆")

    def test_utf8_bom_inside_bzip2_strips_bom(self):
        reader = _load_reader()
        payload = "﻿name,score\nada,9\n".encode("utf-8-sig")
        with tempfile.TemporaryDirectory() as root:
            packed = os.path.join(root, "bom.csv.bz2")
            _write_compressed(packed, payload)
            df = reader.detect_and_load(packed)["bom"]
            self.assertEqual(list(df.columns), ["name", "score"])
            self.assertEqual(df.iloc[0]["score"], 9)

    def test_header_only_input_yields_empty_frame(self):
        reader = _load_reader()
        with tempfile.TemporaryDirectory() as root:
            packed = os.path.join(root, "head.csv.xz")
            _write_compressed(packed, b"alpha,beta\n")
            df = reader.detect_and_load(packed)["head"]
            self.assertEqual(len(df), 0)
            self.assertEqual(list(df.columns), ["alpha", "beta"])

    def test_quoted_comma_and_unicode_fields_round_trip(self):
        reader = _load_reader()
        payload = 'id,note\n1,"hello, world"\n2,"她说：""好"""\n'.encode("utf-8")
        with tempfile.TemporaryDirectory() as root:
            packed = os.path.join(root, "q.csv.gz")
            _write_compressed(packed, payload)
            df = reader.detect_and_load(packed)["q"]
            self.assertEqual(df.iloc[0]["note"], "hello, world")
            self.assertEqual(df.iloc[1]["note"], '她说："好"')

    def test_source_file_bytes_are_preserved(self):
        reader = _load_reader()
        with tempfile.TemporaryDirectory() as root:
            packed = os.path.join(root, "keep.tsv.bz2")
            _write_compressed(packed, b"a\tb\n1\t2\n")
            before = hashlib.md5(Path(packed).read_bytes()).hexdigest()
            reader.detect_and_load(packed)
            result = subprocess.run(
                [sys.executable, SCRIPT, packed, "--json"],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            after = hashlib.md5(Path(packed).read_bytes()).hexdigest()
            self.assertEqual(before, after)

    def test_corrupt_stream_raises_actionable_value_error(self):
        reader = _load_reader()
        with tempfile.TemporaryDirectory() as root:
            packed = os.path.join(root, "bad.csv.gz")
            Path(packed).write_bytes(b"this is not a gzip stream at all")
            with self.assertRaises(ValueError) as ctx:
                reader.detect_and_load(packed)
            message = str(ctx.exception)
            self.assertIn(packed, message)
            self.assertIn("corrupt", message)

    def test_cli_error_path_for_corrupt_stream(self):
        with tempfile.TemporaryDirectory() as root:
            packed = os.path.join(root, "bad.csv.gz")
            Path(packed).write_bytes(b"\x00" * 64)
            result = subprocess.run(
                [sys.executable, SCRIPT, packed],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("ERROR:", result.stderr)

    def test_cli_json_output_for_compressed_tsv(self):
        with tempfile.TemporaryDirectory() as root:
            packed = os.path.join(root, "data.tsv.gz")
            _write_compressed(packed, b"city\ttemp\noslo\t21\n")
            result = subprocess.run(
                [sys.executable, SCRIPT, packed, "--json"],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertIn("data", report["structure"])
            row = report["structure"]["data"]["preview"][0]
            self.assertEqual(row["city"], "oslo")

    def test_plain_excel_and_unsupported_controls_unchanged(self):
        reader = _load_reader()
        with tempfile.TemporaryDirectory() as root:
            plain_csv = os.path.join(root, "p.csv")
            plain_tsv = os.path.join(root, "p.tsv")
            Path(plain_csv).write_bytes(b"x,y\n1,2\n")
            Path(plain_tsv).write_bytes(b"x\ty\n1\t2\n")
            self.assertEqual(list(reader.detect_and_load(plain_csv)), ["p"])
            self.assertEqual(list(reader.detect_and_load(plain_tsv)), ["p"])

            # Legacy .xls rejection is unchanged.
            xls = os.path.join(root, "legacy.xls")
            Path(xls).write_bytes(b"\xd0\xcf\x11\xe0")
            with self.assertRaises(ValueError) as ctx:
                reader.detect_and_load(xls)
            self.assertIn("legacy binary format", str(ctx.exception))

            # ZIP archives and compressed non-table files are not this feature.
            for name in ("a.tar.gz", "b.json.gz", "c.zip", "d.xlsx.gz",
                         "e.csv.zip", "f.gz"):
                unsupported = os.path.join(root, name)
                Path(unsupported).write_bytes(b"\x00" * 8)
                with self.assertRaises(ValueError, msg=name) as ctx:
                    reader.detect_and_load(unsupported)
                self.assertIn("Unsupported file format", str(ctx.exception), name)

            # Real .xlsx still routes through read_excel when openpyxl exists.
            try:
                import openpyxl  # noqa: F401
            except ImportError:
                raise unittest.SkipTest("openpyxl not installed")
            book = os.path.join(root, "book.xlsx")
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = "S1"
            ws.append(["m", "n"])
            ws.append([3, 4])
            wb.save(book)
            sheets = reader.detect_and_load(book)
            self.assertEqual(list(sheets.keys()), ["S1"])


if __name__ == "__main__":
    unittest.main()
