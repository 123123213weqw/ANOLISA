#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for install-claude-code.sh settings.json handling.

Regression tests for the --config write path:

1. `write_config()` used to replace ~/.claude/settings.json with a
   document containing only the four `env` keys, destroying every other
   setting (permissions, hooks, statusLine, ...) of an existing Claude
   Code installation.
2. The API key was interpolated raw into a JSON heredoc, so keys
   containing `"` or `\\` produced syntactically invalid JSON that
   Claude Code refuses to load.

The installer is driven end-to-end with `curl` and `rpm` stubbed on PATH
(no network, no package manager, no root), the same hermetic setup used
by the config-permission tests.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("install-claude-code.sh")

# `curl https://claude.ai/install.sh | bash` — the stub's stdout is the
# "installer", which places a fake claude binary.
CURL_STUB = """#!/bin/sh
mkdir -p "$HOME/.local/bin"
printf '%s' '#!/bin/sh
echo "fake-claude 0.0.0"
' > "$HOME/.local/bin/claude"
chmod +x "$HOME/.local/bin/claude"
"""

RPM_STUB = """#!/bin/sh
exit 0
"""

EXISTING_SETTINGS = {
    "permissions": {"allow": ["Bash(ls:*)"]},
    "statusLine": {"command": "echo hi"},
}

WEIRD_KEY = 'sk-weird "key" \\ end'


class ClaudeCodeConfigMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.stubs = self.root / "bin"
        self.stubs.mkdir()
        for name, body in (("curl", CURL_STUB), ("rpm", RPM_STUB)):
            path = self.stubs / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)

    def run_installer(self, api_key):
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        env["CLAUDE_API_KEY"] = api_key
        env["PATH"] = f"{self.stubs}:{env['PATH']}"
        return subprocess.run(
            ["/bin/bash", str(SCRIPT), "--config", "--skip-tokenless"],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def settings_path(self):
        return self.home / ".claude" / "settings.json"

    def test_existing_settings_survive_config_write(self):
        """--config must merge env keys, not replace the document."""
        claude_dir = self.home / ".claude"
        claude_dir.mkdir()
        self.settings_path().write_text(
            json.dumps(EXISTING_SETTINGS), encoding="utf-8"
        )

        result = self.run_installer("sk-plainkey")
        self.assertEqual(result.returncode, 0, result.stderr)

        data = json.loads(self.settings_path().read_text(encoding="utf-8"))
        self.assertEqual(
            data.get("permissions"), EXISTING_SETTINGS["permissions"]
        )
        self.assertEqual(data.get("statusLine"), EXISTING_SETTINGS["statusLine"])

    def test_env_keys_written_and_escaped(self):
        """The four env keys land verbatim, with real JSON escaping."""
        claude_dir = self.home / ".claude"
        claude_dir.mkdir()
        self.settings_path().write_text(
            json.dumps(EXISTING_SETTINGS), encoding="utf-8"
        )

        result = self.run_installer(WEIRD_KEY)
        self.assertEqual(result.returncode, 0, result.stderr)

        raw = self.settings_path().read_text(encoding="utf-8")
        data = json.loads(raw)  # raises if the escaping is broken
        env = data.get("env", {})
        self.assertEqual(env.get("ANTHROPIC_AUTH_TOKEN"), WEIRD_KEY)
        self.assertEqual(
            env.get("ANTHROPIC_BASE_URL"),
            "https://dashscope.aliyuncs.com/apps/anthropic",
        )
        self.assertEqual(env.get("ANTHROPIC_MODEL"), "qwen3-coder-plus")
        self.assertEqual(
            env.get("ANTHROPIC_SMALL_FAST_MODEL"), "qwen3-coder-plus"
        )


if __name__ == "__main__":
    unittest.main()
