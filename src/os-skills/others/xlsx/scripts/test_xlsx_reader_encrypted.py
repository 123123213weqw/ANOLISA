#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Regression tests for the optional known-password path in xlsx_reader.py.

The reader used to fail on every password-protected OOXML workbook even when
the password was known, forcing users to keep an unprotected copy around for
readonly analysis. These tests build real Agile-encrypted Office workbooks
with msoffcrypto-tool (Unicode sheet names, quoted CJK labels, numbers,
dates, a Unicode password), then run both the loader and the actual CLI:
plain/encrypted dataframe parity, named sheet selection, uppercase XLSM,
missing backend, unchanged source bytes, wrong password, damaged payloads
and credential handling through a named environment variable. No network,
no Excel installation, no decrypted files on disk.
"""

import datetime
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

try:
    import msoffcrypto
    from msoffcrypto.format.ooxml import OOXMLFile
except ImportError:  # backend under test is itself optional
    msoffcrypto = None

try:
    import openpyxl
except ImportError:  # fixture builder
    openpyxl = None

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(SCRIPTS_DIR, "xlsx_reader.py")
PASSWORD = "s3cret-密码"
PASSWORD_ENV = "XLSX_READER_TEST_PASSWORD"


def _load_reader():
    spec = importlib.util.spec_from_file_location("xlsx_reader_enc_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build_plain_bytes() -> bytes:
    """Two worksheets (ASCII + Unicode name), CJK labels, numbers, a date."""
    wb = openpyxl.Workbook()
    alpha = wb.active
    alpha.title = "Alpha"
    alpha.append(["city", "temp"])
    alpha.append(["oslo", 21.5])
    alpha.append(["stockholm", 19.0])

    second = wb.create_sheet("第二")
    second.append(["标签,带逗号", '她说："好"'])
    second.append([42, datetime.datetime(2024, 3, 14)])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _write_plain(path: str) -> None:
    with open(path, "wb") as fh:
        fh.write(_build_plain_bytes())


def _write_encrypted(path: str, password: str = PASSWORD) -> None:
    plain = io.BytesIO(_build_plain_bytes())
    encrypted = io.BytesIO()
    OOXMLFile(plain).encrypt(password, encrypted)
    with open(path, "wb") as fh:
        fh.write(encrypted.getvalue())


def _run_cli(*args: str, timeout: int = 120, env_extra: dict | None = None):
    env = dict(os.environ)
    env.pop(PASSWORD_ENV, None)  # the credential must come only from env_extra
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        capture_output=True, text=True, timeout=timeout, env=env,
    )


class EncryptedWorkbookTests(unittest.TestCase):
    """Known-password OOXML workbooks analyze without an unprotected copy."""

    @classmethod
    def setUpClass(cls):
        if msoffcrypto is None:
            raise unittest.SkipTest("optional msoffcrypto backend not installed")
        if openpyxl is None:
            raise unittest.SkipTest("openpyxl not installed; cannot build fixtures")

    def test_all_worksheets_decrypted_in_workbook_order(self):
        reader = _load_reader()
        with tempfile.TemporaryDirectory() as root:
            book = os.path.join(root, "encrypted.xlsx")
            _write_encrypted(book)
            # The loader takes the env var name, not the password value.
            os.environ[PASSWORD_ENV] = PASSWORD
            try:
                sheets = reader.detect_and_load(book, password_env=PASSWORD_ENV)
            finally:
                del os.environ[PASSWORD_ENV]
            self.assertEqual(list(sheets.keys()), ["Alpha", "第二"])
            self.assertEqual(sheets["Alpha"].iloc[0]["city"], "oslo")

    def test_named_unicode_sheet_selection(self):
        reader = _load_reader()
        os.environ[PASSWORD_ENV] = PASSWORD
        try:
            with tempfile.TemporaryDirectory() as root:
                book = os.path.join(root, "encrypted.xlsx")
                _write_encrypted(book)
                sheets = reader.detect_and_load(book, sheet_name_filter="第二", password_env=PASSWORD_ENV)
        finally:
            del os.environ[PASSWORD_ENV]
        self.assertEqual(list(sheets.keys()), ["第二"])
        self.assertEqual(len(sheets["第二"]), 1)

    def test_plain_encrypted_dataframe_parity(self):
        import pandas as pd

        reader = _load_reader()
        with tempfile.TemporaryDirectory() as root:
            plain = os.path.join(root, "plain.xlsx")
            book = os.path.join(root, "encrypted.xlsx")
            _write_plain(plain)
            _write_encrypted(book)
            os.environ[PASSWORD_ENV] = PASSWORD
            try:
                from_plain = reader.detect_and_load(plain)
                from_encrypted = reader.detect_and_load(book, password_env=PASSWORD_ENV)
            finally:
                del os.environ[PASSWORD_ENV]
            self.assertEqual(sorted(from_plain), sorted(from_encrypted))
            for name in from_plain:
                pd.testing.assert_frame_equal(from_plain[name], from_encrypted[name])

    def test_uppercase_xlsm_suffix(self):
        reader = _load_reader()
        os.environ[PASSWORD_ENV] = PASSWORD
        try:
            with tempfile.TemporaryDirectory() as root:
                book = os.path.join(root, "workbook.XLSM")
                _write_encrypted(book)
                sheets = reader.detect_and_load(book, password_env=PASSWORD_ENV)
        finally:
            del os.environ[PASSWORD_ENV]
        self.assertEqual(list(sheets.keys()), ["Alpha", "第二"])

    def test_source_bytes_unchanged_after_analysis(self):
        reader = _load_reader()
        os.environ[PASSWORD_ENV] = PASSWORD
        try:
            with tempfile.TemporaryDirectory() as root:
                book = os.path.join(root, "encrypted.xlsx")
                _write_encrypted(book)
                before = hashlib.sha256(open(book, "rb").read()).hexdigest()
                reader.detect_and_load(book, password_env=PASSWORD_ENV)
                after = hashlib.sha256(open(book, "rb").read()).hexdigest()
        finally:
            del os.environ[PASSWORD_ENV]
        self.assertEqual(before, after)

    def test_encrypted_without_password_env_is_actionable(self):
        reader = _load_reader()
        with tempfile.TemporaryDirectory() as root:
            book = os.path.join(root, "encrypted.xlsx")
            _write_encrypted(book)
            with self.assertRaises(ValueError) as ctx:
                reader.detect_and_load(book)
            self.assertIn("encrypted", str(ctx.exception))
            self.assertIn("--password-env", str(ctx.exception))

    def test_missing_environment_variable_is_actionable(self):
        reader = _load_reader()
        with tempfile.TemporaryDirectory() as root:
            book = os.path.join(root, "encrypted.xlsx")
            _write_encrypted(book)
            os.environ.pop(PASSWORD_ENV, None)
            with self.assertRaises(ValueError) as ctx:
                reader.detect_and_load(book, password_env=PASSWORD_ENV)
            self.assertIn(PASSWORD_ENV, str(ctx.exception))

    def test_wrong_password_is_actionable(self):
        reader = _load_reader()
        os.environ[PASSWORD_ENV] = "not-the-password"
        try:
            with tempfile.TemporaryDirectory() as root:
                book = os.path.join(root, "encrypted.xlsx")
                _write_encrypted(book)
                with self.assertRaises(ValueError) as ctx:
                    reader.detect_and_load(book, password_env=PASSWORD_ENV)
        finally:
            del os.environ[PASSWORD_ENV]
        self.assertIn("password", str(ctx.exception).lower())

    def test_damaged_encrypted_payload_is_actionable(self):
        reader = _load_reader()
        os.environ[PASSWORD_ENV] = PASSWORD
        try:
            with tempfile.TemporaryDirectory() as root:
                book = os.path.join(root, "encrypted.xlsx")
                _write_encrypted(book)
                raw = bytearray(open(book, "rb").read())
                for i in range(600, 1200):
                    raw[i] = 0
                open(book, "wb").write(bytes(raw))
                with self.assertRaises(ValueError) as ctx:
                    reader.detect_and_load(book, password_env=PASSWORD_ENV)
        finally:
            del os.environ[PASSWORD_ENV]
        self.assertIn("encrypted", str(ctx.exception).lower())

    def test_missing_backend_is_actionable(self):
        reader = _load_reader()
        os.environ[PASSWORD_ENV] = PASSWORD
        blocked = sys.modules.get("msoffcrypto")
        sys.modules["msoffcrypto"] = None  # None entry makes import raise ImportError
        try:
            with tempfile.TemporaryDirectory() as root:
                book = os.path.join(root, "encrypted.xlsx")
                _write_encrypted(book)
                with self.assertRaises(RuntimeError) as ctx:
                    reader.detect_and_load(book, password_env=PASSWORD_ENV)
        finally:
            if blocked is None:
                sys.modules.pop("msoffcrypto", None)
            else:
                sys.modules["msoffcrypto"] = blocked
            del os.environ[PASSWORD_ENV]
        self.assertIn("msoffcrypto", str(ctx.exception))


class EncryptedWorkbookCLITests(unittest.TestCase):
    """The CLI keeps the password out of arguments and reports."""

    @classmethod
    def setUpClass(cls):
        if msoffcrypto is None:
            raise unittest.SkipTest("optional msoffcrypto backend not installed")
        if openpyxl is None:
            raise unittest.SkipTest("openpyxl not installed; cannot build fixtures")

    def test_json_analysis_with_password_env(self):
        with tempfile.TemporaryDirectory() as root:
            book = os.path.join(root, "encrypted.xlsx")
            _write_encrypted(book)
            result = _run_cli(book, "--json", "--password-env", PASSWORD_ENV,
                              env_extra={PASSWORD_ENV: PASSWORD})
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(sorted(payload["structure"].keys()), ["Alpha", "第二"])
            self.assertNotIn(PASSWORD, result.stdout)
            self.assertNotIn(PASSWORD, result.stderr)

    def test_named_sheet_via_cli(self):
        with tempfile.TemporaryDirectory() as root:
            book = os.path.join(root, "encrypted.xlsx")
            _write_encrypted(book)
            result = _run_cli(book, "--json", "--sheet", "第二", "--password-env", PASSWORD_ENV,
                              env_extra={PASSWORD_ENV: PASSWORD})
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(list(payload["structure"].keys()), ["第二"])

    def test_wrong_password_exits_one_with_actionable_message(self):
        with tempfile.TemporaryDirectory() as root:
            book = os.path.join(root, "encrypted.xlsx")
            _write_encrypted(book)
            result = _run_cli(book, "--password-env", PASSWORD_ENV,
                              env_extra={PASSWORD_ENV: "not-the-password"})
            self.assertEqual(result.returncode, 1)
            self.assertIn("password", result.stderr.lower())

    def test_missing_env_var_exits_one_with_actionable_message(self):
        with tempfile.TemporaryDirectory() as root:
            book = os.path.join(root, "encrypted.xlsx")
            _write_encrypted(book)
            result = _run_cli(book, "--password-env", PASSWORD_ENV)
            self.assertEqual(result.returncode, 1)
            self.assertIn(PASSWORD_ENV, result.stderr)

    def test_plain_workbook_still_works_without_option(self):
        with tempfile.TemporaryDirectory() as root:
            plain = os.path.join(root, "plain.xlsx")
            _write_plain(plain)
            result = _run_cli(plain, "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(sorted(payload["structure"].keys()), ["Alpha", "第二"])


if __name__ == "__main__":
    unittest.main()
