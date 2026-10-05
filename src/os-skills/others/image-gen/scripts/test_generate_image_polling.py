#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for the native wanx task poller in generate_image.py.

Regression test: the polling loop kept requesting the task for all 120
iterations after the service reported the documented CANCELED status
and then printed a misleading "Timeout", and a polling HTTPError,
URLError or socket timeout escaped _wanx as a live traceback instead
of the script's standard ERROR reporting.

All HTTP requests and sleeps are fixtures; no network call is made.
"""

import contextlib
import importlib.util
import io
import socket
import sys
import unittest
import urllib.error
from pathlib import Path


SCRIPT = Path(__file__).with_name("generate_image.py")

_spec = importlib.util.spec_from_file_location("generate_image_fixture", SCRIPT)
gi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gi)


class FakeResponse:
    def __init__(self, payload):
        self._body = io.BytesIO(payload.encode("utf-8"))

    def read(self):
        return self._body.read()

    def readable(self):
        return True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def submit_ok(task_id="task-abc"):
    return FakeResponse(f'{{"output":{{"task_id":"{task_id}"}},"request_id":"r"}}')


def poll(status, message="", results=None):
    import json

    output = {"task_status": status}
    if message:
        output["message"] = message
    if results is not None:
        output["results"] = results
    return FakeResponse(json.dumps({"output": output}))


def poll_succeeded(url):
    return FakeResponse(
        '{"output":{"task_status":"SUCCEEDED","results":[{"url":"' + url + '"}]}}'
    )


class WanxPollingTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.poll_responses = []
        self.submitted = False

        def fake_urlopen(req, timeout=None):
            self.calls.append((str(req.full_url), getattr(req, "method", "GET")))
            if not self.submitted:
                self.submitted = True
                return submit_ok()
            action = self.poll_responses.pop(0)
            if isinstance(action, Exception):
                raise action
            return action

        self._real_urlopen = urllib.request.urlopen
        self._real_sleep = gi.time.sleep
        urllib.request.urlopen = fake_urlopen
        gi.time.sleep = lambda seconds: None
        self.addCleanup(self._restore)

    def _restore(self):
        urllib.request.urlopen = self._real_urlopen
        gi.time.sleep = self._real_sleep

    def run_wanx(self):
        """Run _wanx with a fixture key; capture stderr and SystemExit."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                gi._wanx("a prompt", "wanx2.1-t2i-turbo", "1024*1024", "k")
            return caught.exception.code, err.getvalue()
        return None, err.getvalue()

    def run_wanx_value(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            value = gi._wanx("a prompt", "wanx2.1-t2i-turbo", "1024*1024", "k")
        return value, err.getvalue()

    # --- regression: CANCELED must terminate polling ---

    def test_canceled_task_stops_polling_immediately(self):
        # Enough fixture responses for the unfixed loop to exhaust itself.
        self.poll_responses = [poll("CANCELED") for _ in range(120)]
        code, stderr = self.run_wanx()
        self.assertEqual(code, 1)
        self.assertIn("CANCELED", stderr)
        self.assertNotIn("Timeout", stderr)
        # Submit plus exactly one poll; not the full 120-request sweep.
        self.assertEqual(len(self.calls), 2, self.calls)

    # --- regressions: poll transport failures must not leak ---

    def test_poll_http_error_reports_and_exits(self):
        self.poll_responses = [
            urllib.error.HTTPError(
                "https://dashscope.invalid/task", 429, "Too Many Requests",
                {}, io.BytesIO(b'{"code":"Throttling"}'),
            )
        ]
        code, stderr = self.run_wanx()
        self.assertEqual(code, 1)
        self.assertIn("HTTP 429", stderr)

    def test_poll_url_error_reports_and_exits(self):
        self.poll_responses = [
            urllib.error.URLError(ConnectionError("reset by peer"))
        ]
        code, stderr = self.run_wanx()
        self.assertEqual(code, 1)
        self.assertIn("ERROR", stderr)
        self.assertIn("reset by peer", stderr)

    def test_poll_socket_timeout_reports_and_exits(self):
        self.poll_responses = [socket.timeout("timed out")]
        code, stderr = self.run_wanx()
        self.assertEqual(code, 1)
        self.assertIn("ERROR", stderr)
        self.assertIn("timed out", stderr)

    # --- controls: existing contracts preserved ---

    def test_succeeded_returns_image_url(self):
        self.poll_responses = [
            poll("PENDING"), poll("RUNNING"), poll_succeeded("https://img/x.png")
        ]
        value, _ = self.run_wanx_value()
        self.assertEqual(value, "https://img/x.png")
        self.assertEqual(len(self.calls), 4, self.calls)

    def test_success_after_repeated_pending_polls(self):
        self.poll_responses = [poll("PENDING") for _ in range(5)] + [poll_succeeded("https://img/y.png")]
        value, _ = self.run_wanx_value()
        self.assertEqual(value, "https://img/y.png")
        self.assertEqual(len(self.calls), 7, self.calls)

    def test_failed_task_exits_with_service_message(self):
        self.poll_responses = [poll("FAILED", message="content policy violation")]
        code, stderr = self.run_wanx()
        self.assertEqual(code, 1)
        self.assertIn("content policy violation", stderr)


if __name__ == "__main__":
    unittest.main()
