#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Regression tests for args/env/headers option-shape validation in validate_mcp.py.

Covers the malformed option families that previously validated OK and were
published by --merge before any check: string/null/non-string args entries,
non-mapping env/headers, and numeric/null mapping values. Refusal must happen
before the destination is read, written or created; diagnostics must name the
server and field without echoing credential contents; valid empty collections,
omitted fields and arbitrary string values must keep merging exactly.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(SCRIPTS_DIR, "validate_mcp.py")

SECRET = "SECRET-TOKEN-do-not-echo-3f9c21"


def _run(argv):
    return subprocess.run([sys.executable, SCRIPT, *argv],
                          capture_output=True, text=True)


def _server(**overrides):
    cfg = {"command": "/usr/bin/fake-mcp", "args": ["--stdio"],
           "env": {"LANG": "C"}, "headers": {"X-Api-Key": "k"}}
    cfg.update(overrides)
    return {"mcpServers": {"srv": cfg}}


def _write_config(tmp, obj, name="config.json"):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.write("\n")
    return path


class OptionShapeValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="validate_mcp_")
        self.config = _write_config(self.tmp, _server())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_check_rejects_malformed_args_family(self):
        cases = [
            ("args is a string", {"args": "--stdio"}),
            ("args holds null", {"args": ["--stdio", None]}),
            ("args holds non-strings", {"args": ["--port", 8080]}),
        ]
        for label, override in cases:
            with self.subTest(label):
                path = _write_config(self.tmp, _server(**override), name=f"args_{label.replace(' ', '_')}.json")
                proc = _run(["--check", path])
                self.assertNotEqual(proc.returncode, 0, f"{label}: check must fail")
                self.assertIn("[srv]", proc.stderr)
                self.assertIn("args", proc.stderr)

    def test_check_rejects_non_mapping_and_bad_value_env_headers(self):
        cases = [
            ("env is a list", {"env": ["LANG"]}),
            ("env is a string", {"env": "LANG=C"}),
            ("env numeric value", {"env": {"PORT": 8080}}),
            ("env null value", {"env": {"TOKEN": None}}),
            ("headers is a list", {"headers": ["X-Api-Key"]}),
            ("headers numeric value", {"headers": {"X-Api-Key": 42}}),
            ("headers null value", {"headers": {"Authorization": None}}),
        ]
        for label, override in cases:
            with self.subTest(label):
                path = _write_config(self.tmp, _server(**override), name=f"eh_{label.replace(' ', '_')}.json")
                proc = _run(["--check", path])
                self.assertNotEqual(proc.returncode, 0, f"{label}: check must fail")
                self.assertIn("[srv]", proc.stderr)
                self.assertIn("env" in override and "env" or "headers", proc.stderr)

    def test_check_diagnostic_does_not_echo_credential_values(self):
        # Malformed args forces diagnostics while env carries a real secret:
        # the error must name the server and field, never the values.
        path = _write_config(self.tmp, _server(args=[None], env={"API_TOKEN": SECRET}))
        proc = _run(["--check", path])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("[srv]", proc.stderr)
        self.assertNotIn(SECRET, proc.stderr)
        self.assertNotIn(SECRET, proc.stdout)

    def test_check_accepts_valid_empty_and_omitted_options(self):
        path = _write_config(self.tmp, _server(args=[], env={}, headers={}))
        proc = _run(["--check", path])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("OK: 1 server(s)", proc.stdout)
        omitted = _write_config(self.tmp, {"mcpServers": {"srv": {"command": "/usr/bin/fake-mcp"}}},
                                name="omitted.json")
        proc = _run(["--check", omitted])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Whitespace-only and empty string values stay valid.
        ws = _write_config(self.tmp, _server(env={"A": " ", "B": ""}, args=[" "], headers={"H": ""}),
                           name="whitespace.json")
        proc = _run(["--check", ws])
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_merge_refuses_malformed_before_touching_existing_destination(self):
        with open(self.config, "rb") as fh:
            before = fh.read()
        payload = _server(env={"API_TOKEN": 12345})
        proc = _run([json.dumps(payload), "--merge", self.config])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("[srv]", proc.stderr)
        self.assertNotIn("Written to", proc.stderr)
        with open(self.config, "rb") as fh:
            self.assertEqual(before, fh.read(), "refused merge must leave destination untouched")

    def test_merge_refuses_malformed_without_creating_new_destination(self):
        target = os.path.join(self.tmp, "nested", "does", "not", "exist.json")
        payload = _server(args=["ok", None])
        proc = _run([json.dumps(payload), "--merge", target])
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(os.path.exists(target), "refused merge must not create the destination")
        self.assertFalse(os.path.exists(os.path.dirname(target)))

    def test_merge_valid_string_options_exactly(self):
        target = os.path.join(self.tmp, "fresh.json")
        payload = _server(args=["--stdio", "--verbose"], env={"LANG": "C.UTF-8"},
                          headers={"X-Api-Key": "k", "Authorization": "Bearer t"})
        proc = _run([json.dumps(payload), "--merge", target])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(target, "r", encoding="utf-8") as fh:
            written = json.load(fh)
        self.assertEqual(written["mcpServers"]["srv"], payload["mcpServers"]["srv"])
        self.assertIn("Written to", proc.stderr)


if __name__ == "__main__":
    unittest.main()
