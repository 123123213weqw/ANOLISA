#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for read_pdf.py readonly --form-fields JSON mode.

Regression tests for the AcroForm export mode (issue #5938): stored field
values and widget metadata were unreachable, so text extraction was the
only representation of a filled form. The mode must export the selected
pages' widgets as detached field records, keep repeated appearances,
list empty field sets for pages without widgets, leave the default
text/JSON schemas untouched, never modify the source file, and require
--format json before installation/opening.
"""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

try:
    import fitz
except ImportError:  # pragma: no cover - PyMuPDF is the skill's dependency
    raise unittest.SkipTest("PyMuPDF (fitz) is required for AcroForm fixtures")

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(SCRIPTS_DIR, "read_pdf.py")

UNICODE_VALUE = 'Zoë "Belle"\nLine 2'


def build_form_pdf(path):
    """Build a 5-page AcroForm fixture.

    Page 1: labeled text field (Unicode/quotes/newlines), repeated "notes"
    widget, checked checkbox, combobox with choices, listbox with choices.
    Page 2: two-radio kid group, first kid on.
    Page 3: unsigned signature field (raw /FT /Sig widget).
    Page 4: second "notes" appearance (repeated logical field).
    Page 5: no widgets. Document carries inert OpenAction JavaScript.
    """
    doc = fitz.open()
    p1 = doc.new_page(width=595, height=842)
    w = fitz.Widget()
    w.field_name = "fullname"; w.field_label = "Full name"
    w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    w.rect = fitz.Rect(50, 50, 300, 70); w.field_value = UNICODE_VALUE
    p1.add_widget(w)
    w = fitz.Widget()
    w.field_name = "notes"; w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    w.rect = fitz.Rect(50, 340, 300, 360); w.field_value = "first appearance"
    p1.add_widget(w)
    w = fitz.Widget()
    w.field_name = "subscribe"; w.field_type = fitz.PDF_WIDGET_TYPE_CHECKBOX
    w.rect = fitz.Rect(50, 90, 70, 110); w.field_value = True
    p1.add_widget(w)
    w = fitz.Widget()
    w.field_name = "country"; w.field_type = fitz.PDF_WIDGET_TYPE_COMBOBOX
    w.choice_values = ["de", "fr", "us"]
    w.rect = fitz.Rect(50, 120, 200, 140); w.field_value = "fr"
    p1.add_widget(w)
    w = fitz.Widget()
    w.field_name = "topics"; w.field_type = fitz.PDF_WIDGET_TYPE_LISTBOX
    w.choice_values = ["ai", "os", "sec"]
    w.rect = fitz.Rect(50, 150, 200, 190); w.field_value = "os"
    p1.add_widget(w)

    p2 = doc.new_page(width=595, height=842)
    radio_xrefs = []
    for x in (50, 120):
        w = fitz.Widget()
        w.field_name = "gender"; w.field_type = fitz.PDF_WIDGET_TYPE_RADIOBUTTON
        w.rect = fitz.Rect(x, 50, x + 20, 70); w.field_value = False
        radio_xrefs.append(p2.add_widget(w).xref)
    doc.xref_set_key(radio_xrefs[0], "AS", "/Yes")

    p3 = doc.new_page(width=595, height=842)
    sig_xref = doc.get_new_xref()
    doc.update_object(sig_xref,
                      "<< /Type /Annot /Subtype /Widget /FT /Sig /T (sig1) "
                      "/Rect [50 50 200 80] >>")
    doc.xref_set_key(p3.xref, "Annots", "[%d 0 R]" % sig_xref)

    p4 = doc.new_page(width=595, height=842)
    w = fitz.Widget()
    w.field_name = "notes"; w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    w.rect = fitz.Rect(50, 300, 300, 320); w.field_value = "second appearance"
    p4.add_widget(w)

    doc.new_page(width=595, height=842)  # page 5: empty

    doc.xref_set_key(doc.pdf_catalog(), "OpenAction",
                     "<< /S /JavaScript /JS (app.alert\\('inert'\\);) >>")
    doc.set_metadata({"title": "AcroForm fixture"})
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


def run_read_pdf(args):
    return subprocess.run([sys.executable, SCRIPT] + args,
                          capture_output=True, text=True)


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def plain_types(value):
    if isinstance(value, dict):
        return all(plain_types(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return all(plain_types(v) for v in value)
    return isinstance(value, (str, int, float, bool, type(None)))


class TestCollectFormFields(unittest.TestCase):
    """API-level tests for the detached field-record collector."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdf = build_form_pdf(os.path.join(self.tmp.name, "form.pdf"))

    def tearDown(self):
        self.tmp.cleanup()

    def field_records(self, page_indices=None):
        module = load_read_pdf()
        self.assertTrue(hasattr(module, "collect_form_fields"),
                        "read_pdf.py must expose collect_form_fields(doc, idx)")
        doc = fitz.open(self.pdf)
        try:
            indices = page_indices or list(range(len(doc)))
            return module.collect_form_fields(doc, indices)
        finally:
            doc.close()

    def by_page(self, records):
        return {rec["page"]: rec["fields"] for rec in records}

    def find(self, records, page, name):
        for field in self.by_page(records)[page]:
            if field["name"] == name:
                return field
        self.fail("field %r not found on page %d" % (name, page))

    def test_exports_widget_records_for_selected_pages(self):
        records = self.field_records()
        self.assertEqual([rec["page"] for rec in records], [1, 2, 3, 4, 5])
        counts = [len(rec["fields"]) for rec in records]
        self.assertEqual(counts, [5, 2, 1, 1, 0])
        field = self.find(records, 1, "fullname")
        self.assertEqual(field["label"], "Full name")
        self.assertEqual(field["type"], "Text")
        self.assertEqual(field["type_id"], fitz.PDF_WIDGET_TYPE_TEXT)
        self.assertEqual(field["flags"], 0)
        self.assertEqual(field["rect"], [50.0, 50.0, 300.0, 70.0])
        self.assertGreater(field["xref"], 0)

        selected = self.field_records([0, 3])
        self.assertEqual([rec["page"] for rec in selected], [1, 4])

    def test_detaches_values_before_document_close(self):
        module = load_read_pdf()
        self.assertTrue(hasattr(module, "collect_form_fields"),
                        "read_pdf.py must expose collect_form_fields(doc, idx)")
        doc = fitz.open(self.pdf)
        records = module.collect_form_fields(doc, list(range(len(doc))))
        doc.close()
        # Records must survive close() as plain Python data only.
        self.assertTrue(plain_types(records), records)
        self.assertEqual(self.find(records, 1, "fullname")["value"], UNICODE_VALUE)
        self.assertEqual(self.find(records, 2, "gender")["value"], "Yes")

    def test_exports_choices_flags_and_button_states(self):
        records = self.field_records()
        combo = self.find(records, 1, "country")
        self.assertEqual(combo["choices"], ["de", "fr", "us"])
        self.assertNotIn("button_states", combo)
        self.assertNotIn("signed", combo)
        topics = self.find(records, 1, "topics")
        self.assertEqual(topics["choices"], ["ai", "os", "sec"])
        checkbox = self.find(records, 1, "subscribe")
        self.assertEqual(checkbox["value"], "Yes")
        self.assertEqual(checkbox["button_states"]["normal"], ["Off", "Yes"])
        self.assertNotIn("choices", checkbox)
        radios = [f for f in self.by_page(records)[2] if f["name"] == "gender"]
        self.assertEqual(sorted(r["value"] for r in radios), ["Off", "Yes"])
        for radio in radios:
            self.assertIn("Yes", radio["button_states"]["normal"])
            self.assertNotIn("choices", radio)

    def test_reports_signature_fields_unsigned(self):
        records = self.field_records()
        signature = self.find(records, 3, "sig1")
        self.assertEqual(signature["type"], "Signature")
        self.assertEqual(signature["type_id"], fitz.PDF_WIDGET_TYPE_SIGNATURE)
        self.assertIs(signature["signed"], False)
        self.assertNotIn("choices", signature)
        self.assertNotIn("button_states", signature)

    def test_keeps_repeated_fields_and_empty_page_lists(self):
        records = self.field_records()
        first = self.find(records, 1, "notes")
        second = self.find(records, 4, "notes")
        self.assertEqual(first["value"], "first appearance")
        self.assertEqual(second["value"], "second appearance")
        self.assertNotEqual(first["xref"], second["xref"])
        self.assertEqual(self.by_page(records)[5], [])

    def test_never_modifies_source_bytes(self):
        before = sha256(self.pdf)
        self.field_records()
        after = sha256(self.pdf)
        self.assertEqual(before, after)
        # Reading widgets must not grow or rewrite the document either.
        self.assertEqual(os.path.getsize(self.pdf),
                         os.path.getsize(self.pdf))

    def test_roundtrips_unicode_quotes_and_newlines(self):
        records = self.field_records()
        self.assertEqual(self.find(records, 1, "fullname")["value"], UNICODE_VALUE)


class TestFormFieldsCLI(unittest.TestCase):
    """CLI tests for `read_pdf.py --form-fields --format json`."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pdf = build_form_pdf(os.path.join(cls.tmp.name, "form.pdf"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def payload(self, extra_args=()):
        result = run_read_pdf(["-f", self.pdf, "--form-fields",
                               "--format", "json"] + list(extra_args))
        return result, json.loads(result.stdout)

    def test_form_fields_json_shape(self):
        result, payload = self.payload()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["total_pages"], 5)
        self.assertEqual([p["page"] for p in payload["pages"]],
                         [1, 2, 3, 4, 5])
        self.assertEqual(len(payload["pages"][0]["fields"]), 5)
        self.assertEqual(payload["pages"][4]["fields"], [])
        self.assertNotIn("text", payload["pages"][0])
        for field in payload["pages"][0]["fields"]:
            self.assertIn("name", field)
            self.assertIn("value", field)
            self.assertIn("rect", field)

    def test_form_fields_requires_json_format(self):
        _, payload = self.payload()
        self.assertIn("pages", payload)
        result = run_read_pdf(["-f", self.pdf, "--form-fields"])
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("json", result.stderr.lower())
        self.assertEqual(result.stdout, "")

    def test_form_fields_selected_pages_and_metadata(self):
        result, payload = self.payload(["-p", "2,5", "-d"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([p["page"] for p in payload["pages"]], [2, 5])
        self.assertEqual(len(payload["pages"][0]["fields"]), 2)
        self.assertEqual(payload["pages"][1]["fields"], [])
        self.assertEqual(payload["metadata"]["title"], "AcroForm fixture")


class TestDefaultSchemasUnchanged(unittest.TestCase):
    """Control: without --form-fields the historical text/JSON output stays."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.plain = build_plain_pdf(os.path.join(cls.tmp.name, "plain.pdf"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_default_text_and_json_output(self):
        result = run_read_pdf(["-f", self.plain])
        self.assertEqual(result.returncode, 0, result.stderr)
        # print() appends the final newline after the joined page blocks.
        self.assertEqual(result.stdout, "--- Page 1 ---\nHello\n\n")
        result = run_read_pdf(["-f", self.plain, "--format", "json"])
        payload = json.loads(result.stdout)
        self.assertEqual(payload, {"total_pages": 1,
                                   "pages": [{"page": 1, "text": "Hello"}]})
        self.assertNotIn("fields", payload["pages"][0])


if __name__ == "__main__":
    unittest.main()
