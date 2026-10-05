#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for read_pdf.py readonly embedded-attachment discovery/extraction.

Regression tests for the embedded-file workflow (issue #5950): the PDF
reader could neither discover document-level embedded files nor recover
their payloads, so page text extraction silently omitted attached datasets
and supporting documents. The mode must expose JSON discovery records with
physical zero-based indices and SDK metadata only (never loading payloads),
plus index-selected extraction that publishes the exact bytes as a new
atomically-created file, never replacing an existing destination (including
the source), never using attachment names as filesystem paths, never
executing or modifying content, and leaving no partial artifact on any
error path. Default text/JSON and page selection stay unchanged.
"""

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

try:
    import fitz
except ImportError:  # pragma: no cover - PyMuPDF is the skill's dependency
    raise unittest.SkipTest("PyMuPDF (fitz) is required for attachment fixtures")

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(SCRIPTS_DIR, "read_pdf.py")

UTF8_PAYLOAD = "héllo,wörld\nsecond line\n".encode("utf-8")
BINARY_PAYLOAD = b"\x00\x01\x02PDF-bin\xff\xfe"
EMPTY_PAYLOAD = b""
PAGE_TEXT = "Embedded fixture page"


def build_attachment_pdf(path):
    """Build a 1-page fixture carrying three document-level embedded files.

    Index 0: UTF-8 payload named "dataset". Index 1: binary payload that is
    renamed to the duplicate name "dataset" via the catalog /Names array
    (embfile_add refuses duplicates), proving physical indices stay
    unambiguous. Index 2: empty payload. Names are never used as paths.
    """
    doc = fitz.open()
    doc.new_page(width=595, height=842).insert_text((72, 72), PAGE_TEXT)
    doc.embfile_add("dataset", UTF8_PAYLOAD,
                    filename="a.csv", ufilename="a.csv",
                    desc="primary dataset")
    doc.embfile_add("dataset-copy", BINARY_PAYLOAD,
                    filename="b.zip", ufilename="b.zip",
                    desc="duplicate name carrier")
    doc.embfile_add("empty.txt", EMPTY_PAYLOAD,
                    filename="e.txt", ufilename="e.txt",
                    desc="empty attachment")
    kind, val = doc.xref_get_key(doc.pdf_catalog(), "Names/EmbeddedFiles/Names")
    assert kind == "array" and "(dataset-copy)" in val, val
    doc.xref_set_key(doc.pdf_catalog(), "Names/EmbeddedFiles/Names",
                     val.replace("(dataset-copy)", "(dataset)", 1))
    doc.set_metadata({"title": "attachment fixture"})
    doc.save(path)
    doc.close()
    return path


def build_plain_pdf(path):
    doc = fitz.open()
    doc.new_page(width=595, height=842).insert_text((72, 72), "Hello")
    doc.save(path)
    doc.close()
    return path


def load_read_pdf():
    spec = importlib.util.spec_from_file_location("read_pdf_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_read_pdf(args, cwd=None):
    return subprocess.run([sys.executable, SCRIPT] + args,
                          capture_output=True, text=True, cwd=cwd)


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def plain_types(value):
    if isinstance(value, dict):
        return all(plain_types(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return all(plain_types(v) for v in value)
    return isinstance(value, (str, int, float, bool, type(None)))


class AttachmentFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdf = build_attachment_pdf(
            os.path.join(self.tmp.name, "fixture.pdf"))
        self.pdf_sha = sha256(self.pdf)

    def tearDown(self):
        self.tmp.cleanup()

    def assertSourceUntouched(self):
        self.assertEqual(sha256(self.pdf), self.pdf_sha,
                         "the source PDF must never be modified")


class TestCollectAttachmentRecords(AttachmentFixture):
    """API-level tests for the metadata-only discovery collector."""

    def test_exposes_physical_zero_based_records(self):
        module = load_read_pdf()
        self.assertTrue(hasattr(module, "collect_attachment_records"),
                        "read_pdf.py must expose collect_attachment_records(doc)")
        doc = fitz.open(self.pdf)
        try:
            records = module.collect_attachment_records(doc)
        finally:
            doc.close()
        self.assertEqual([r["index"] for r in records], [0, 1, 2])
        self.assertEqual([r["name"] for r in records],
                         ["dataset", "dataset", "empty.txt"])
        first = records[0]
        self.assertEqual(first["filename"], "a.csv")
        self.assertEqual(first["ufilename"], "a.csv")
        self.assertEqual(first["description"], "primary dataset")
        self.assertEqual(first["size"], len(UTF8_PAYLOAD))
        self.assertEqual(records[1]["size"], len(BINARY_PAYLOAD))
        self.assertEqual(records[2]["size"], 0)
        self.assertTrue(plain_types(records), records)
        self.assertSourceUntouched()

    def test_records_survive_document_close(self):
        module = load_read_pdf()
        doc = fitz.open(self.pdf)
        records = module.collect_attachment_records(doc)
        doc.close()
        self.assertTrue(plain_types(records), records)
        self.assertEqual(records[1]["name"], "dataset")

    def test_discovery_never_loads_payloads(self):
        module = load_read_pdf()
        doc = fitz.open(self.pdf)
        with mock.patch.object(fitz.Document, "embfile_get",
                               side_effect=AssertionError(
                                   "discovery must not load payloads")):
            records = module.collect_attachment_records(doc)
        doc.close()
        self.assertEqual(len(records), 3)


class TestDiscoveryCli(AttachmentFixture):
    def test_cli_discovery_json_lists_all_records(self):
        r = run_read_pdf(["-f", self.pdf, "--attachments", "--format", "json"])
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["total_attachments"], 3)
        self.assertEqual([a["index"] for a in out["attachments"]], [0, 1, 2])
        names = [a["name"] for a in out["attachments"]]
        self.assertEqual(names.count("dataset"), 2)
        self.assertEqual(out["attachments"][2]["description"],
                         "empty attachment")
        self.assertSourceUntouched()

    def test_cli_discovery_requires_json_format(self):
        r = run_read_pdf(["-f", self.pdf, "--attachments"])
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertIn("--format json", r.stderr)
        self.assertSourceUntouched()

    def test_cli_discovery_honors_max_length(self):
        r = run_read_pdf(["-f", self.pdf, "--attachments",
                          "--format", "json", "-m", "40"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.rstrip().endswith("...[truncated]"), r.stdout)


class TestExtractionCli(AttachmentFixture):
    def extract(self, index, name, extra=None):
        out = os.path.join(self.tmp.name, name)
        argv = ["-f", self.pdf, "--extract-attachment", str(index),
                "--output", out, "--format", "json"]
        r = run_read_pdf(argv + (extra or []))
        return r, out

    def read_bytes(self, path):
        with open(path, "rb") as fh:
            return fh.read()

    def test_extracts_exact_binary_payload(self):
        r, out = self.extract(1, "extracted.zip")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read_bytes(out), BINARY_PAYLOAD)
        summary = json.loads(r.stdout)
        self.assertEqual(summary["index"], 1)
        self.assertEqual(summary["output"], os.path.abspath(out))
        self.assertEqual(summary["bytes"], len(BINARY_PAYLOAD))
        self.assertSourceUntouched()

    def test_extracts_utf8_and_empty_payloads(self):
        r, out = self.extract(0, "data.csv")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read_bytes(out), UTF8_PAYLOAD)

        r, out = self.extract(2, "empty-out.txt")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read_bytes(out), b"")
        self.assertEqual(os.path.getsize(out), 0)
        self.assertSourceUntouched()

    def test_supports_unicode_destination_paths(self):
        subdir = os.path.join(self.tmp.name, "Zoë 目录")
        os.mkdir(subdir)
        out = os.path.join(subdir, "附件.bin")
        r = run_read_pdf(["-f", self.pdf, "--extract-attachment", "1",
                          "--output", out, "--format", "json"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read_bytes(out), BINARY_PAYLOAD)

    def test_never_replaces_existing_destination(self):
        out = os.path.join(self.tmp.name, "existing.bin")
        with open(out, "wb") as fh:
            fh.write(b"precious")
        r, _ = self.extract(0, "existing.bin")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("already exists", r.stderr)
        self.assertEqual(self.read_bytes(out), b"precious")
        self.assertSourceUntouched()

    def test_never_replaces_the_source_pdf(self):
        r, _ = self.extract(0, "fixture.pdf")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("already exists", r.stderr)
        self.assertSourceUntouched()

    def test_invalid_index_is_actionable(self):
        r = run_read_pdf(["-f", self.pdf, "--extract-attachment", "99",
                          "--output", os.path.join(self.tmp.name, "x.bin"),
                          "--format", "json"])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("index", r.stderr)
        self.assertIn("3", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "x.bin")))
        self.assertSourceUntouched()

    def test_incomplete_or_incompatible_options_error(self):
        out = os.path.join(self.tmp.name, "x.bin")
        base = ["-f", self.pdf]
        r = run_read_pdf(base + ["--extract-attachment", "0", "--format", "json"])
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("--output", r.stderr)
        r = run_read_pdf(base + ["--output", out, "--format", "json"])
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("--extract-attachment", r.stderr)
        r = run_read_pdf(base + ["--attachments", "--extract-attachment", "0",
                                 "--output", out, "--format", "json"])
        self.assertEqual(r.returncode, 2, r.stdout)
        r = run_read_pdf(base + ["--extract-attachment", "zero",
                                 "--output", out, "--format", "json"])
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertFalse(os.path.exists(out))

    def test_no_partial_artifact_on_publication_failure(self):
        ro = os.path.join(self.tmp.name, "readonly")
        os.mkdir(ro)
        os.chmod(ro, 0o500)
        try:
            out = os.path.join(ro, "partial.bin")
            r = run_read_pdf(["-f", self.pdf, "--extract-attachment", "0",
                              "--output", out, "--format", "json"])
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("ERROR", r.stderr)
            self.assertEqual(os.listdir(ro), [])
        finally:
            os.chmod(ro, 0o700)

    def test_missing_output_directory_is_actionable(self):
        r = run_read_pdf(["-f", self.pdf, "--extract-attachment", "0",
                          "--output", os.path.join(self.tmp.name, "no", "x.bin"),
                          "--format", "json"])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("directory", r.stderr)


class TestExtractAttachmentApi(AttachmentFixture):
    def test_concurrent_destination_race_leaves_no_artifact(self):
        module = load_read_pdf()
        self.assertTrue(hasattr(module, "extract_attachment"),
                        "read_pdf.py must expose extract_attachment(doc, index, path)")
        out = os.path.join(self.tmp.name, "raced.bin")
        doc = fitz.open(self.pdf)
        with mock.patch("os.link", side_effect=FileExistsError(17, "File exists")):
            with self.assertRaises(ValueError) as ctx:
                module.extract_attachment(doc, 0, out)
        doc.close()
        self.assertIn("already exists", str(ctx.exception))
        self.assertFalse(os.path.exists(out))
        self.assertEqual(os.listdir(self.tmp.name),
                         sorted(["fixture.pdf"]))
        self.assertSourceUntouched()

    def test_publication_failure_wrapped_actionable(self):
        module = load_read_pdf()
        out = os.path.join(self.tmp.name, "quota.bin")
        doc = fitz.open(self.pdf)
        with mock.patch("os.link", side_effect=OSError(122, "Disk quota exceeded")):
            with self.assertRaises(ValueError) as ctx:
                module.extract_attachment(doc, 0, out)
        doc.close()
        self.assertIn("failed to publish", str(ctx.exception))
        self.assertFalse(os.path.exists(out))
        self.assertEqual(os.listdir(self.tmp.name), sorted(["fixture.pdf"]))


class TestDefaultModesUnchanged(AttachmentFixture):
    """Controls: pre-existing text/JSON behavior stays byte-identical."""

    def test_default_text_mode_unchanged(self):
        r = run_read_pdf(["-f", self.pdf])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--- Page 1 ---", r.stdout)
        self.assertIn(PAGE_TEXT, r.stdout)
        self.assertNotIn("dataset", r.stdout)
        self.assertSourceUntouched()

    def test_default_json_mode_unchanged(self):
        r = run_read_pdf(["-f", self.pdf, "--format", "json"])
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["total_pages"], 1)
        self.assertEqual(out["pages"][0]["text"], PAGE_TEXT)
        self.assertNotIn("attachments", out)
        self.assertNotIn("total_attachments", out)
        self.assertSourceUntouched()

    def test_plain_pdf_reports_zero_attachments(self):
        plain = build_plain_pdf(os.path.join(self.tmp.name, "plain.pdf"))
        r = run_read_pdf(["-f", plain, "--attachments", "--format", "json"])
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["total_attachments"], 0)
        self.assertEqual(out["attachments"], [])


if __name__ == "__main__":
    unittest.main()
