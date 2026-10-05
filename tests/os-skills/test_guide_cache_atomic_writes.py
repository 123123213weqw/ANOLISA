#!/usr/bin/env python3
"""Regression tests for atomic guide cache artifact writes (#5627).

Drives the actual ``save_markdown_file`` / ``crawl_all_docs`` from
``crawl_docs.py`` (network stubbed, no HTTP) with injected partial-write
and failed-replace faults. On unchanged main a mid-write failure truncates
the previously complete artifact to a prefix; with the staging publisher
the old bytes survive and no partial file is left behind.

Baseline on unchanged main: 4 failures / 3 passing controls / 2 Windows-only
tests skipped on POSIX.
"""

import builtins
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "os-skills"
    / "others"
    / "anolisa-guide"
    / "scripts"
    / "crawl_docs.py"
)

OLD_DOC = "# previous complete guide\n" + ("line of cached content\n" * 40)
REAL_OPEN = builtins.open


def load_module():
    spec = importlib.util.spec_from_file_location("crawl_docs_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _PartialFile:
    """File wrapper whose first write emits a prefix, then fails."""

    def __init__(self, handle, prefix):
        self._handle = handle
        self._prefix = prefix

    def write(self, data):
        self._handle.write(data[: self._prefix])
        raise OSError("simulated disk full")

    def __enter__(self):
        self._handle.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._handle.__exit__(*exc_info)

    def __getattr__(self, item):
        return getattr(self._handle, item)


def partial_write_patches(basename, prefix=48):
    """Intercept open(), os.fdopen() and tempfile.mkstemp() for `basename`.

    open() handles carry the final path as ``.name``; fdopen() handles only
    carry the file descriptor, so staging descriptors are tracked via the
    mkstemp() call that created them.
    """
    import tempfile as tempfile_module

    stack = ExitStack()
    real_mkstemp = tempfile_module.mkstemp
    real_fdopen = os.fdopen
    watched_fds = set()

    def fake_open(file, mode="r", *args, **kwargs):
        handle = REAL_OPEN(file, mode, *args, **kwargs)
        name = getattr(handle, "name", "")
        if isinstance(name, str) and basename in os.path.basename(name):
            return _PartialFile(handle, prefix)
        return handle

    def fake_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        if basename in os.path.basename(name):
            watched_fds.add(fd)
        return fd, name

    def fake_fdopen(fd, mode="r", *args, **kwargs):
        handle = real_fdopen(fd, mode, *args, **kwargs)
        if fd in watched_fds:
            watched_fds.discard(fd)
            return _PartialFile(handle, prefix)
        return handle

    stack.enter_context(mock.patch("builtins.open", fake_open))
    stack.enter_context(mock.patch("tempfile.mkstemp", fake_mkstemp))
    stack.enter_context(mock.patch("os.fdopen", fake_fdopen))
    return stack


class AtomicWriteTestCase(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.enterContext(mock.patch.object(self.module.time, "sleep"))
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def save(self, name, title="New Title", content="fresh crawl content\n" * 30):
        data = {
            "url": "/zh/alinux/faq",
            "full_url": "https://example.invalid/zh/alinux/faq",
            "title": title,
            "last_modified": "2026-10-01",
            "content": content,
        }
        return self.module.save_markdown_file(data, self.dir / name)

    def crawl_all_failed(self):
        with mock.patch.object(self.module, "get_page_content", return_value=None):
            return self.module.crawl_all_docs(self.dir)


class TestPartialWritesPreserveOldDocuments(AtomicWriteTestCase):
    def test_partial_markdown_write_preserves_old_document(self):
        target = self.dir / "faq.md"
        target.write_text(OLD_DOC, encoding="utf-8")
        with partial_write_patches("faq.md"):
            with self.assertRaises(OSError):
                self.save("faq.md")
        self.assertEqual(target.read_text(encoding="utf-8"), OLD_DOC)

    def test_partial_summary_write_preserves_old_summary(self):
        summary = self.dir / "crawl_summary.json"
        old_json = json.dumps({"crawl_time": "old", "total_docs": 13})
        summary.write_text(old_json, encoding="utf-8")
        with partial_write_patches("crawl_summary.json"):
            with self.assertRaises(OSError):
                self.crawl_all_failed()
        self.assertEqual(summary.read_text(encoding="utf-8"), old_json)

    def test_failed_final_replace_preserves_old_document(self):
        target = self.dir / "faq.md"
        target.write_text(OLD_DOC, encoding="utf-8")
        with mock.patch.object(os, "replace", side_effect=OSError("replace denied")):
            with self.assertRaises(OSError):
                self.save("faq.md")
        self.assertEqual(target.read_text(encoding="utf-8"), OLD_DOC)
        self.assertEqual(list(self.dir.glob("*.tmp")), [])

    def test_failed_new_artifact_leaves_no_partial_file(self):
        target = self.dir / "faq.md"
        with partial_write_patches("faq.md"):
            with self.assertRaises(OSError):
                self.save("faq.md")
        self.assertFalse(target.exists())
        self.assertEqual(list(self.dir.glob("*.tmp")), [])


class TestCompleteArtifacts(AtomicWriteTestCase):
    def test_complete_markdown_document_written(self):
        target = self.dir / "faq.md"
        target.write_text(OLD_DOC, encoding="utf-8")
        os.chmod(target, 0o640)
        content = self.save("faq.md")
        written = target.read_text(encoding="utf-8")
        self.assertEqual(written, content)
        self.assertIn("# New Title", written)
        self.assertIn("fresh crawl content", written)
        self.assertNotIn("previous complete guide", written)
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o640)

    def test_complete_summary_schema(self):
        self.crawl_all_failed()
        data = json.loads(
            (self.dir / "crawl_summary.json").read_text(encoding="utf-8")
        )
        self.assertEqual(data["total_docs"], len(self.module.DOC_URLS))
        self.assertEqual(data["success_count"], 0)
        self.assertEqual(data["failed_count"], len(self.module.DOC_URLS))
        self.assertEqual(len(data["results"]), len(self.module.DOC_URLS))
        self.assertTrue(all(r["status"] == "failed" for r in data["results"]))

    def test_write_through_symlink_updates_target_only(self):
        real = self.dir / "real.md"
        real.write_text(OLD_DOC, encoding="utf-8")
        link = self.dir / "agentic-os.md"
        link.symlink_to(real)
        self.save("agentic-os.md", title="Linked")
        self.assertTrue(link.is_symlink())
        self.assertIn("# Linked", real.read_text(encoding="utf-8"))


@unittest.skipUnless(sys.platform == "win32", "Windows-only replace semantics")
class TestWindowsReplaceSemantics(AtomicWriteTestCase):
    def test_held_open_target_keeps_old_bytes(self):
        target = self.dir / "faq.md"
        target.write_text(OLD_DOC, encoding="utf-8")
        with target.open("r"):
            # A concurrently held handle makes the final replace fail on
            # Windows; the old document must survive regardless.
            try:
                self.save("faq.md")
            except OSError:
                pass
        self.assertEqual(target.read_text(encoding="utf-8"), OLD_DOC)
        self.assertEqual(list(self.dir.glob("*.tmp")), [])

    def test_readonly_directory_failed_publish_cleans_staging(self):
        target = self.dir / "faq.md"
        target.write_text(OLD_DOC, encoding="utf-8")
        os.chmod(self.dir, 0o500)
        try:
            with self.assertRaises(OSError):
                self.save("faq.md")
        finally:
            os.chmod(self.dir, 0o700)
        self.assertEqual(target.read_text(encoding="utf-8"), OLD_DOC)
        self.assertEqual(list(self.dir.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2, exit=False)
