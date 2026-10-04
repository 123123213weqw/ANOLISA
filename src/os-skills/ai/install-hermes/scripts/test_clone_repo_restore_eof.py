#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for install.sh clone_repo() restore-prompt EOF handling.

Regression test: after stashing local changes and fast-forwarding, the
update path asks "Restore local changes now? [Y/n]" with a bare
`read -r restore_answer` while the installer runs under `set -e`. EOF
on that read (Ctrl-D, terminal going away) returned non-zero and errexit
aborted the whole installer right after a successful update - the stash
was never restored nor mentioned, and none of the remaining install
steps ran.

The scenario drives the real clone_repo() through a pseudo-terminal so
the interactive branch is taken, then delivers EOF to the prompt.
`git stash drop` is neutralized via a PATH wrapper because passing the
recorded object ID to `git stash drop` is tracked separately (#5498).
"""

import os
import pty
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parent
SCRIPT = SCRIPTS_DIR / "install.sh"

# Path wrapper: everything passes through to the real git except
# `git stash drop`, which is out of scope here (see #5498).
GIT_WRAPPER = """#!/bin/sh
if [ "$1" = "stash" ] && [ "$2" = "drop" ]; then
  exit 0
fi
exec "%s" "$@"
"""

# Appended after the script body (whose last line is the bare `main`
# invocation, stripped): run only the update path of clone_repo().
SCENARIO = """
INSTALL_DIR="{install_dir}"
BRANCH="main"
clone_repo
echo "HARNESS-COMPLETED rc=$?"
"""


def run_git(cwd, *args, env=None):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )


class CloneRepoRestoreEofTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git_env = {
            "GIT_AUTHOR_NAME": "audit",
            "GIT_AUTHOR_EMAIL": "audit@example.com",
            "GIT_COMMITTER_NAME": "audit",
            "GIT_COMMITTER_EMAIL": "audit@example.com",
        }
        self.stubs = self.root / "bin"
        self.stubs.mkdir()
        real_git = shutil.which("git")
        wrapper = self.stubs / "git"
        wrapper.write_text(GIT_WRAPPER % real_git, encoding="utf-8")
        wrapper.chmod(0o755)

        # Remote with one commit, then a local checkout carrying an
        # uncommitted modification and a remote that moved ahead.
        self.remote = self.root / "remote.git"
        run_git(self.root, "init", "--bare", str(self.remote))
        seed = self.root / "seed"
        run_git(self.root, "clone", str(self.remote), str(seed), env=self.git_env)
        (seed / "file.txt").write_text("base\n", encoding="utf-8")
        run_git(seed, "add", "file.txt", env=self.git_env)
        run_git(seed, "commit", "-m", "base", env=self.git_env)
        run_git(seed, "push", "origin", "HEAD:refs/heads/main", env=self.git_env)

        self.repo = self.root / "repo"
        run_git(self.root, "clone", str(self.remote), str(self.repo), env=self.git_env)
        # Move the remote ahead so the update path has something to pull.
        (seed / "file.txt").write_text("remote-new\n", encoding="utf-8")
        run_git(seed, "add", "file.txt", env=self.git_env)
        run_git(seed, "commit", "-m", "advance", env=self.git_env)
        run_git(seed, "push", "origin", "HEAD:refs/heads/main", env=self.git_env)
        # Local uncommitted change -> clone_repo takes the autostash path.
        (self.repo / "local.txt").write_text("local edit\n", encoding="utf-8")

        self.harness = self.root / "harness.sh"
        body = SCRIPT.read_text(encoding="utf-8")
        lines = [ln for ln in body.splitlines() if ln.strip() != "main"]
        self.harness.write_text(
            "\n".join(lines) + "\n" + SCENARIO.format(install_dir=self.repo),
            encoding="utf-8",
        )

    def run_harness_on_pty_with_eof(self):
        master, slave = pty.openpty()
        env = dict(os.environ)
        env["PATH"] = f"{self.stubs}:{env['PATH']}"
        env.update(self.git_env)
        proc = subprocess.Popen(
            ["/bin/bash", str(self.harness)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
        )
        os.close(slave)
        chunks = []
        sent = threading.Event()

        def drain():
            # Drain continuously (a full pty buffer would deadlock the
            # child) and deliver Ctrl-D only once the prompt appeared.
            while True:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    return
                if not data:
                    return
                chunks.append(data)
                if not sent.is_set() and b"Restore local changes now?" in b"".join(chunks):
                    os.write(master, b"\x04")
                    sent.set()

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            proc.wait(timeout=60)
        finally:
            proc.kill()
        reader.join(timeout=5)
        os.close(master)
        return proc.returncode, b"".join(chunks).decode("utf-8", errors="replace")

    def test_eof_at_restore_prompt_does_not_abort(self):
        """EOF must fall through to the documented default, not errexit."""
        rc, output = self.run_harness_on_pty_with_eof()
        self.assertIn("Restore local changes now?", output)

        self.assertEqual(rc, 0, output)
        self.assertIn("Repository ready", output)
        # Default answer is "restore": the local edit must be back.
        self.assertEqual(
            (self.repo / "local.txt").read_text(encoding="utf-8"), "local edit\n"
        )

    def test_stash_is_recorded_before_prompt(self):
        """Sanity: the scenario really reached the autostash flow."""
        rc, output = self.run_harness_on_pty_with_eof()
        self.assertIn("Local changes detected, stashing before update...", output)


if __name__ == "__main__":
    unittest.main()
