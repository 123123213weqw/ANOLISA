#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for install.sh HERMES_HOME child-process export (#5544).

Regression test: the installer accepts ``--hermes-home PATH`` and writes
config templates into the selected directory, but the shell variable was
never exported when the environment did not already carry ``HERMES_HOME``.
Child processes (the Python skill sync, the setup wizard and the Hermes
gateway) resolve their data home from ``os.environ["HERMES_HOME"]`` before
the platform default, so a CLI-selected home silently diverged from what
the children used.

The tests drive the REAL installer option parsing: the script body is used
unchanged except that the final ``main`` entry point is replaced by a child
environment recorder, which spawns a fresh interpreter that mimics the
official ``hermes_constants`` data-home resolution
(``os.environ["HERMES_HOME"]`` first, platform ``~/.hermes`` default
second) and records both the raw inherited value and the resolved home.
No packages, config files or services are installed.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
SCRIPT = SCRIPTS_DIR / "install.sh"

# Mirrors hermes_constants.py: the environment wins over the platform
# default when the child resolves its data home.
RECORDER = r'''
# ---- test recorder: replaces the main() installation entry point ----
"{python}" - <<'PYEOF'
import os

home = os.path.expanduser("~")
raw = os.environ.get("HERMES_HOME", "")
resolved = raw if raw else os.path.join(home, ".hermes")
with open({out!r}, "w", encoding="utf-8") as fh:
    fh.write(raw + "\n" + resolved + "\n")
PYEOF
exit 0
'''


def load_script_body_without_main():
    """Return install.sh with the trailing ``main`` invocation stripped."""
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    assert lines and lines[-1].strip() == "main", (
        "install.sh must end with the bare `main` invocation"
    )
    lines.pop()
    return "\n".join(lines) + "\n"


class HermesHomeExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fake_home = self.root / "home"
        self.fake_home.mkdir()
        self.recorder_out = self.root / "child-env.txt"
        self.scenario = self.root / "scenario.sh"
        body = load_script_body_without_main()
        recorder = RECORDER.format(python=sys.executable, out=str(self.recorder_out))
        self.scenario.write_text(body + recorder, encoding="utf-8")

    def run_installer(self, args, extra_env=None):
        """Runs the real option parsing with a controlled environment."""
        env = {
            "HOME": str(self.fake_home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }
        for key in ("HERMES_HOME", "HERMES_INSTALL_DIR"):
            env.pop(key, None)
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run(
            ["bash", str(self.scenario), *args],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(
            proc.returncode, 0, f"installer parsing failed: {proc.stderr}"
        )
        raw, resolved = self.recorder_out.read_text(encoding="utf-8").splitlines()
        return raw, resolved

    def test_cli_selected_home_reaches_child_processes(self):
        selected = str(self.root / "selected-home")
        raw, resolved = self.run_installer(["--hermes-home", selected])
        self.assertEqual(
            raw, selected, "a CLI-selected HERMES_HOME must be exported to children"
        )
        self.assertEqual(resolved, selected)

    def test_cli_selected_home_wins_over_platform_default(self):
        selected = str(self.root / "selected-home")
        raw, resolved = self.run_installer(["--hermes-home", selected])
        platform_default = str(self.fake_home / ".hermes")
        self.assertNotEqual(
            resolved,
            platform_default,
            "the child must not fall back to the platform default "
            "when the installer selected a custom home",
        )

    def test_default_home_is_visible_to_children(self):
        raw, resolved = self.run_installer([])
        self.assertEqual(
            resolved, str(self.fake_home / ".hermes"),
            "without selection the child resolves the default home",
        )

    def test_inherited_home_is_forwarded_unchanged(self):
        inherited = str(self.root / "inherited-home")
        raw, resolved = self.run_installer([], {"HERMES_HOME": inherited})
        self.assertEqual(raw, inherited)
        self.assertEqual(resolved, inherited)

    def test_cli_override_of_exported_home_updates_children(self):
        inherited = str(self.root / "inherited-home")
        selected = str(self.root / "selected-home")
        raw, resolved = self.run_installer(
            ["--hermes-home", selected], {"HERMES_HOME": inherited}
        )
        self.assertEqual(raw, selected)
        self.assertEqual(resolved, selected)


if __name__ == "__main__":
    unittest.main()
