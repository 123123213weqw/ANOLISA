#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for check-env.sh remediation output.

Regression test: the missing-build-directory marker `kernel-build-dir`
is not an installable package, but the summary used to include it in the
suggested `sudo yum install -y ...` command. On Alinux 4 `yum` is dnf,
and dnf fails the whole transaction when one name has no match, so the
script's own remediation advice could never succeed.
"""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("check-env.sh")

# Fake a consistent machine: x86_64, exotic running kernel (so
# /lib/modules/<ver>/build can never exist on the host), rpm reports
# every queried package as not installed.
UNAME_STUB = """#!/bin/sh
case "$1" in
  -m) echo x86_64 ;;
  -r) echo 9.9.999-audit ;;
  *) /usr/bin/uname "$@" ;;
esac
"""

RPM_STUB = """#!/bin/sh
exit 1
"""


class CheckEnvInstallHintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stubs = Path(self.tmp.name)
        for name, body in (("uname", UNAME_STUB), ("rpm", RPM_STUB)):
            path = self.stubs / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)

    def run_check_env(self):
        env = dict(os.environ)
        env["PATH"] = f"{self.stubs}:{env['PATH']}"
        return subprocess.run(
            ["/bin/bash", str(SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_suggested_install_command_has_no_marker_entry(self):
        """The yum command must contain only real package names."""
        result = self.run_check_env()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("kernel-build-dir", result.stdout)  # still reported missing

        cmd_lines = re.findall(r"^\s*(sudo yum install -y .*)$", result.stdout, re.M)
        self.assertTrue(cmd_lines, result.stdout)
        for cmd in cmd_lines:
            self.assertNotIn("kernel-build-dir", cmd)
        # The real packages stay in the suggestion.
        self.assertIn("gcc", cmd_lines[0])
        self.assertIn("kernel-devel-9.9.999-audit", cmd_lines[0])

    def test_build_directory_hint_prints_only_when_directory_missing(self):
        """The dedicated hint follows the marker, not the summary block.

        With the stubbed exotic kernel the build directory is always
        missing, so the end-to-end run must print the hint. A
        plain-packages-only failure (marker absent from MISSING) must
        not print it; exercising that end-to-end would need an existing
        /lib/modules/<kver>/build (not creatable hermetically), so the
        summary branch is sourced directly with a synthetic MISSING.
        """
        result = self.run_check_env()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("For the missing kernel build directory:", result.stdout)
        self.assertIn("kernel-devel-9.9.999-audit", result.stdout)

        summary = Path(SCRIPT).read_text(encoding="utf-8").split("# Summary", 1)[1]
        lines = summary.splitlines()
        start = next(i for i, ln in enumerate(lines) if "package(s):" in ln)
        end = next(
            i for i in range(len(lines) - 1, start, -1) if lines[i].strip() == "fi"
        )
        else_branch = "\n".join(lines[start:end]) + "\n"

        def run_summary(missing):
            prelude = (
                "set -euo pipefail\n"
                "RED='' GREEN='' YELLOW='' NC=''\n"
                f"MISSING=({' '.join(missing)})\n"
                "ARCH=x86_64\nKERNEL_VER=9.9.999-audit\n"
            )
            return subprocess.run(
                ["/bin/bash", "-c", prelude + else_branch],
                capture_output=True,
                text=True,
                env={"PATH": "/usr/bin:/bin", "TERM": "dumb"},
                timeout=30,
            )

        with_marker = run_summary(["gcc", "kernel-build-dir"])
        self.assertEqual(with_marker.returncode, 1, with_marker.stdout)
        self.assertIn("For the missing kernel build directory:", with_marker.stdout)

        without_marker = run_summary(
            ["gcc", "kernel-devel-9.9.999-audit", "kernel-headers-9.9.999-audit"]
        )
        self.assertEqual(without_marker.returncode, 1, without_marker.stdout)
        self.assertIn("sudo yum install -y gcc", without_marker.stdout)
        self.assertNotIn(
            "For the missing kernel build directory:", without_marker.stdout
        )


if __name__ == "__main__":
    unittest.main()
