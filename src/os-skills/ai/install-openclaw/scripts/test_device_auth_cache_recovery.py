#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for install_openclaw.py cached device-auth recovery.

Regression tests for syntactically valid but non-object
identity/device-auth.json roots (null, list, string, boolean, number),
which used to crash cached_operator_has_write_scope and
clear_cached_operator_device_auth with AttributeError because both called
data.get() unconditionally. Non-object roots must behave like malformed
cached authorization: the read-only scope check returns False and recovery
uses the byte-preserving backup-before-delete path.
"""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(SCRIPTS_DIR, "install_openclaw.py")

_spec = importlib.util.spec_from_file_location("install_openclaw_under_test", SCRIPT)
install_openclaw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install_openclaw)


def load_module():
    return install_openclaw


class DeviceAuthCacheTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config = self.root / "openclaw" / "config.yaml"
        self.config.parent.mkdir(parents=True)
        self.config.write_text("gateway:\n  port: 1\n", encoding="utf-8")
        self.identity = self.config.parent / "identity"
        self.identity.mkdir()
        self.cache = self.identity / "device-auth.json"
        self.args = SimpleNamespace(config=str(self.config))

    def write_cache(self, payload):
        if isinstance(payload, str):
            self.cache.write_text(payload, encoding="utf-8")
        else:
            self.cache.write_text(json.dumps(payload), encoding="utf-8")

    def test_non_object_roots_do_not_crash_scope_check(self):
        module = load_module()
        for document in ("null", "[]", '"device"', "true", "3.5"):
            with self.subTest(root=document):
                self.write_cache(document)
                self.assertIs(module.cached_operator_has_write_scope(self.args), False)

    def test_non_object_roots_recover_via_backup_before_delete(self):
        module = load_module()
        for document in ("null", "[]", '"device"', "true", "3.5"):
            with self.subTest(root=document):
                self.write_cache(document)
                original = self.cache.read_bytes()
                module.clear_cached_operator_device_auth(self.args)
                self.assertFalse(self.cache.exists())
                backup = self.identity / "device-auth.json.bak"
                self.assertTrue(backup.exists())
                self.assertEqual(backup.read_bytes(), original)

    def test_valid_write_scope_tokens_are_trusted(self):
        module = load_module()
        self.write_cache(
            {
                "tokens": {
                    "operator": {"scopes": ["operator.read", "operator.write"]}
                },
                "unknown_field": {"kept": True},
            }
        )
        self.assertIs(module.cached_operator_has_write_scope(self.args), True)

    def test_admin_scope_and_role_form_are_trusted(self):
        module = load_module()
        self.write_cache({"role": "operator", "scopes": ["operator.admin"]})
        self.assertIs(module.cached_operator_has_write_scope(self.args), True)

    def test_read_only_scope_is_not_write_scope(self):
        module = load_module()
        self.write_cache({"role": "operator", "scopes": ["operator.read"]})
        self.assertIs(module.cached_operator_has_write_scope(self.args), False)

    def test_operator_token_reset_preserves_other_tokens(self):
        module = load_module()
        self.write_cache(
            {
                "tokens": {
                    "operator": {"scopes": ["operator.read"]},
                    "other": {"scopes": ["other.write"]},
                }
            }
        )
        module.clear_cached_operator_device_auth(self.args)
        data = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertNotIn("operator", data["tokens"])
        self.assertIn("other", data["tokens"])

    def test_operator_role_cache_is_deleted(self):
        module = load_module()
        self.write_cache({"role": "operator", "scopes": ["operator.read"]})
        module.clear_cached_operator_device_auth(self.args)
        self.assertFalse(self.cache.exists())

    def test_malformed_json_is_backed_up_and_deleted(self):
        module = load_module()
        self.write_cache("{not json")
        original = self.cache.read_bytes()
        module.clear_cached_operator_device_auth(self.args)
        self.assertFalse(self.cache.exists())
        backup = self.identity / "device-auth.json.bak"
        self.assertEqual(backup.read_bytes(), original)

    def test_absent_cache_is_untouched(self):
        module = load_module()
        self.assertFalse(self.cache.exists())
        self.assertIs(module.cached_operator_has_write_scope(self.args), False)
        module.clear_cached_operator_device_auth(self.args)
        self.assertFalse(self.cache.exists())
        self.assertFalse((self.identity / "device-auth.json.bak").exists())

    def test_failed_backup_leaves_cache_intact(self):
        module = load_module()
        self.write_cache("null")
        # A directory occupying the backup name makes the byte-preserving
        # backup fail; the original cache must survive that failure.
        backup_dir = self.identity / "device-auth.json.bak"
        backup_dir.mkdir()
        with self.assertRaises(OSError):
            module.clear_cached_operator_device_auth(self.args)
        self.assertTrue(self.cache.exists())
        self.assertEqual(self.cache.read_text(encoding="utf-8"), "null")


if __name__ == "__main__":
    unittest.main()
