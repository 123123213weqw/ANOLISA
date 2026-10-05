#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for install_openclaw.py model-reply preflight validation.

Regression tests for the pre-flight protocol gap (issue #5942): a
successful HTTP status was treated as proof of a valid model reply, so
proxy login HTML, JSON null, truncated/wrong-protocol objects and
provider errors delivered with HTTP 200 all passed pre-flight and let
configuration be written. The reply must now be read bounded, parsed as
complete UTF-8 JSON and validated against the minimum Anthropic
Messages or OpenAI Chat Completions success envelope before any
configuration is published.
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(SCRIPTS_DIR, "install_openclaw.py")

LIMIT = 64 * 1024

HTML_LOGIN = b"<!DOCTYPE html><html><body>proxy login page</body></html>"

ANTHROPIC_EMPTY = json.dumps(
    {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": "qwen3.6-plus",
        "content": [],
        "stop_reason": "max_tokens",
        "usage": {"input_tokens": 10, "output_tokens": 1},
    }
).encode("utf-8")

OPENAI_TOOL_REPLY = json.dumps(
    {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "",
                            "tool_calls": [{"id": "t1", "type": "function"}]},
                "finish_reason": "tool_calls",
            }
        ],
    }
).encode("utf-8")

PROVIDER_ERROR = json.dumps(
    {"type": "error",
     "error": {"type": "authentication_error", "message": "invalid api key"}}
).encode("utf-8")


def load_installer():
    spec = importlib.util.spec_from_file_location("install_openclaw_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReplyServer:
    """Local HTTP server standing in for the model endpoint."""

    def __init__(self, status, body, content_type="application/json"):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.requests += 1
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.requests = 0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()


def run_installer(server, tmp, extra=()):
    config_path = os.path.join(tmp, "openclaw.json")
    result = subprocess.run(
        [sys.executable, SCRIPT,
         "--config", config_path,
         "--api-key", "test-key-123",
         "--base-url", f"http://127.0.0.1:{server.port}",
         "--model-id", "qwen3.6-plus",
         "--skip-install-openclaw", "--skip-gateway", "--skip-tokenless",
         "--skip-gateway-write-check", "--preflight-timeout", "10"] + list(extra),
        capture_output=True, text=True, timeout=90)
    return result, config_path


class TestReadModelReply(unittest.TestCase):
    def reader(self):
        module = load_installer()
        self.assertTrue(hasattr(module, "read_model_reply"),
                        "install_openclaw.py must expose read_model_reply(response)")
        return module

    def test_reads_up_to_the_bound_and_rejects_overflow(self):
        module = self.reader()
        exact = io.BytesIO(b"x" * LIMIT)
        self.assertEqual(len(module.read_model_reply(exact)), LIMIT)
        oversized = io.BytesIO(b"x" * (LIMIT + 1))
        with self.assertRaises(module.PreflightReplyError):
            module.read_model_reply(oversized)

    def test_default_bound_is_64kib(self):
        module = self.reader()
        self.assertEqual(module.PREFLIGHT_MAX_REPLY_BYTES, LIMIT)


class TestValidateModelReply(unittest.TestCase):
    def validator(self):
        module = load_installer()
        self.assertTrue(hasattr(module, "validate_model_reply"),
                        "install_openclaw.py must expose validate_model_reply(data)")
        return module

    def assert_rejected(self, data):
        module = self.validator()
        with self.assertRaises(module.PreflightReplyError):
            module.validate_model_reply(data)

    def assert_accepted(self, data):
        module = self.validator()
        module.validate_model_reply(data)  # must not raise

    def test_rejects_html_login_page(self):
        self.assert_rejected(HTML_LOGIN)

    def test_rejects_json_null_arrays_strings_and_truncated_json(self):
        self.assert_rejected(b"null")
        self.assert_rejected(b"[1, 2, 3]")
        self.assert_rejected(b'"ok"')
        self.assert_rejected(ANTHROPIC_EMPTY[: len(ANTHROPIC_EMPTY) // 2])

    def test_rejects_invalid_utf8(self):
        self.assert_rejected(b"\xff\xfe{\"id\": \"x\"}")

    def test_rejects_provider_error_envelopes(self):
        self.assert_rejected(PROVIDER_ERROR)
        self.assert_rejected(json.dumps({"error": {"message": "quota exceeded"}}).encode())

    def test_rejects_wrong_protocol_objects(self):
        embeddings = json.dumps({"object": "list", "data": [{"id": "e", "object": "embedding"}]}).encode()
        self.assert_rejected(embeddings)
        chunk = json.dumps({"id": "c1", "object": "chat.completion.chunk",
                            "choices": []}).encode()
        self.assert_rejected(chunk)

    def test_accepts_minimum_anthropic_envelope_without_text(self):
        self.assert_accepted(ANTHROPIC_EMPTY)

    def test_accepts_openai_tool_and_empty_replies(self):
        self.assert_accepted(OPENAI_TOOL_REPLY)
        self.assert_accepted(json.dumps({"id": "c2", "object": "chat.completion",
                                         "choices": []}).encode())


class TestPreflightDispatch(unittest.TestCase):
    """End-to-end argparse/main dispatch against a local endpoint."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_html_login_reply_stops_configuration(self):
        with ReplyServer(200, HTML_LOGIN, "text/html") as server:
            result, config_path = run_installer(server, self.tmp.name)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(os.path.exists(config_path),
                         "invalid reply must block configuration publication")

    def test_json_null_reply_stops_configuration(self):
        with ReplyServer(200, b"null") as server:
            result, config_path = run_installer(server, self.tmp.name)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(os.path.exists(config_path))

    def test_provider_error_with_http_200_stops_configuration(self):
        with ReplyServer(200, PROVIDER_ERROR) as server:
            result, config_path = run_installer(server, self.tmp.name)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("invalid api key", result.stderr + result.stdout)
        self.assertFalse(os.path.exists(config_path))

    def test_oversized_reply_stops_configuration(self):
        huge = b'{"padding": "' + b"a" * (LIMIT + 4096) + b'"}'
        with ReplyServer(200, huge) as server:
            result, config_path = run_installer(server, self.tmp.name)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(os.path.exists(config_path))

    def test_rejected_reply_is_not_dumped(self):
        marker = "proxy login page"
        with ReplyServer(200, HTML_LOGIN, "text/html") as server:
            result, _ = run_installer(server, self.tmp.name)
        combined = result.stdout + result.stderr
        self.assertNotIn(marker, combined)

    def test_valid_anthropic_reply_publishes_config(self):
        with ReplyServer(200, ANTHROPIC_EMPTY) as server:
            result, config_path = run_installer(server, self.tmp.name)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.exists(config_path))
        with open(config_path, encoding="utf-8") as fh:
            published = json.load(fh)
        provider = published["models"]["providers"]["bailian"]
        self.assertEqual(provider["apiKey"], "test-key-123")
        self.assertIn("[OK] API key", result.stdout)

    def test_http_error_keeps_status_and_hint(self):
        with ReplyServer(401, PROVIDER_ERROR) as server:
            result, config_path = run_installer(server, self.tmp.name)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        combined = result.stdout + result.stderr
        self.assertIn("HTTP 401", combined)
        self.assertIn("Authentication failed", combined)
        self.assertIn("invalid api key", combined)
        self.assertFalse(os.path.exists(config_path))

    def test_skip_preflight_flag_is_preserved(self):
        with ReplyServer(200, HTML_LOGIN, "text/html") as server:
            result, config_path = run_installer(server, self.tmp.name,
                                                extra=["--skip-preflight"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(server.requests, 0)
        self.assertTrue(os.path.exists(config_path))


if __name__ == "__main__":
    unittest.main()
