#!/usr/bin/env python3
"""Regression tests for gateway listener PID discovery (#5630).

Drives the actual ``gateway_port_listeners`` from ``install_openclaw.py``
with ``run_command``/``find_command`` replaced by fixtures. No real
process, gateway or installation is touched: every tool result is a
canned ``subprocess.CompletedProcess`` and the PID streams follow the
documented fuser/lsof contract (PIDs on stdout, diagnostics on stderr).

Baseline on unchanged main: 4 failures / 2 passing controls.
"""

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "os-skills"
    / "ai"
    / "install-openclaw"
    / "scripts"
    / "install_openclaw.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("install_openclaw_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GatewayListenerFixture(unittest.TestCase):
    """Stub tool discovery and execution around the real function."""

    def setUp(self):
        self.module = load_module()
        self.args = SimpleNamespace(gateway_port=8080, gateway_status_timeout=5)
        self.commands = []
        self.tools = {}
        self.results = {}

        def fake_find_command(name):
            return self.tools.get(name, "")

        def fake_run_command(cmd, **kwargs):
            self.commands.append(list(cmd))
            key = cmd[0].rsplit("/", 1)[-1] if "/" in cmd[0] else cmd[0]
            outcome = self.results.get(key)
            if outcome is None:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
            stdout, stderr, returncode = outcome
            return subprocess.CompletedProcess(
                cmd, returncode, stdout=stdout, stderr=stderr
            )

        self.module.find_command = fake_find_command
        self.module.run_command = fake_run_command

    def ran_tool(self, name):
        return any(cmd and cmd[0].endswith("/" + name) for cmd in self.commands)

    def discover(self):
        return self.module.gateway_port_listeners(self.args)


class TestPidStreamsOnly(GatewayListenerFixture):
    def test_fuser_numeric_user_metadata_ignored(self):
        """fuser -v prints the USER column (numeric UID) on stderr, PID on stdout."""
        self.tools["fuser"] = "/usr/bin/fuser"
        self.results["fuser"] = ("4242", "8080/tcp:\n501   4242 F.... node", 0)
        self.assertEqual(self.discover(), ["4242"])

    def test_diagnostics_only_fuser_falls_back_to_lsof(self):
        """Diagnostics-only fuser output must not suppress the lsof fallback."""
        self.tools["fuser"] = "/usr/bin/fuser"
        self.tools["lsof"] = "/usr/sbin/lsof"
        self.results["fuser"] = ("", "8080/tcp: 501 F.... (process gone)", 1)
        self.results["lsof"] = ("9999", "", 0)
        self.assertEqual(self.discover(), ["9999"])
        self.assertTrue(self.ran_tool("lsof"))

    def test_lsof_warning_numbers_ignored(self):
        """lsof emits PID numbers on stdout; WARNING lines live on stderr."""
        self.tools["lsof"] = "/usr/sbin/lsof"
        self.results["lsof"] = ("31337", "lsof: WARNING: can't stat() fs 12345", 0)
        self.assertEqual(self.discover(), ["31337"])

    def test_stdout_pid_glued_to_stderr_digits(self):
        """Concatenated streams glue the stdout PID to stderr text and lose it."""
        self.tools["fuser"] = "/usr/bin/fuser"
        self.results["fuser"] = ("4242", "cannot stat 987", 0)
        self.assertEqual(self.discover(), ["4242"])


class TestDiscoveryContract(GatewayListenerFixture):
    def test_duplicate_stdout_pids_deduplicated(self):
        self.tools["lsof"] = "/usr/sbin/lsof"
        self.results["lsof"] = ("1111\n1111 1111", "", 0)
        self.assertEqual(self.discover(), ["1111"])

    def test_fuser_hit_returns_without_lsof(self):
        self.tools["fuser"] = "/usr/bin/fuser"
        self.tools["lsof"] = "/usr/sbin/lsof"
        self.results["fuser"] = ("2222", "", 0)
        self.assertEqual(self.discover(), ["2222"])
        self.assertFalse(self.ran_tool("lsof"))

    def test_unavailable_tools_return_empty(self):
        self.assertEqual(self.discover(), [])
        self.assertEqual(self.commands, [])


if __name__ == "__main__":
    unittest.main(verbosity=2, exit=False)
