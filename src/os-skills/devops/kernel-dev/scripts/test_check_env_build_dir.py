#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for check-env.sh kernel build directory validation.

Regression test: the check accepted any symlink or directory entry at
/lib/modules/$(uname -r)/build. A dangling symlink (kernel-devel
uninstalled after an upgrade) or a directory missing its Kbuild Makefile
was reported with a green check, so the summary claimed kernel modules
could be compiled when they could not.

The inspected build path is redirected to a private fixture by stubbing
`uname -r` to a dot-dot traversal relative to /lib/modules; no system
file, package or compilation is touched.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("check-env.sh")

RPM_STUB = """#!/bin/sh
exit 1
"""


class CheckEnvBuildDirTests(unittest.TestCase):
    def setUp(self):
        if not os.path.isdir("/lib/modules"):
            raise unittest.SkipTest(
                "/lib/modules not present on this host (non-Linux)"
            )
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.stubs = root / "stubs"
        self.stubs.mkdir()
        self.fixture_root = root / "fixture"
        self.fixture_root.mkdir()
        # Relative path from the physical location of /lib/modules so the
        # script's hardcoded "/lib/modules/$KERNEL_VER/build" resolves
        # inside the fixture (on usrmerge systems /lib is a symlink to
        # usr/lib, and ".." resolves from the physical directory).
        modules_base = os.path.realpath("/lib/modules")
        self.modules_rel = os.path.relpath(self.fixture_root, modules_base)
        rpm = self.stubs / "rpm"
        rpm.write_text(RPM_STUB, encoding="utf-8")
        rpm.chmod(0o755)

    def write_uname_stub(self, kernel_ver):
        stub = f"""#!/bin/sh
case "$1" in
  -m) echo x86_64 ;;
  -r) echo '{kernel_ver}' ;;
  *) /usr/bin/uname "$@" ;;
esac
"""
        path = self.stubs / "uname"
        path.write_text(stub, encoding="utf-8")
        path.chmod(0o755)

    def run_check_env(self, scenario):
        self.write_uname_stub(f"{self.modules_rel}/{scenario}")
        env = dict(os.environ)
        env["PATH"] = f"{self.stubs}:{env['PATH']}"
        return subprocess.run(
            ["/bin/bash", str(SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def populated(self, name):
        """A build tree directory containing its Kbuild Makefile."""
        tree = self.fixture_root / name / "tree"
        tree.mkdir(parents=True)
        (tree / "Makefile").write_text("obj-y :=\n", encoding="utf-8")
        return tree

    # --- regressions: unusable build trees must be reported missing ---

    def test_dangling_build_symlink_reported_missing(self):
        """kernel-devel removed: build -> vanished headers tree."""
        scenario = self.fixture_root / "dangling"
        scenario.mkdir()
        (scenario / "build").symlink_to(
            self.fixture_root / "vanished-tree", target_is_directory=True
        )
        result = self.run_check_env("dangling")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertNotIn(" -> ", result.stdout, result.stdout)
        self.assertIn("kernel-build-dir", result.stdout)

    def test_build_directory_without_makefile_reported_missing(self):
        """Headers directory without its Kbuild Makefile cannot build."""
        scenario = self.fixture_root / "no-makefile"
        scenario.mkdir()
        (scenario / "build").mkdir()
        (scenario / "build" / "include").mkdir()
        result = self.run_check_env("no-makefile")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertNotIn(" -> ", result.stdout, result.stdout)
        self.assertIn("kernel-build-dir", result.stdout)

    # --- controls: usable build trees keep passing ---

    def test_valid_real_directory_passes(self):
        tree = self.populated("valid-dir")
        (self.fixture_root / "valid-dir" / "build").symlink_to(
            tree, target_is_directory=True
        )
        result = self.run_check_env("valid-dir")
        self.assertIn(" -> ", result.stdout, result.stdout)
        self.assertIn(str(tree), result.stdout)
        self.assertNotIn("kernel-build-dir", result.stdout)

    def test_valid_symlink_to_populated_tree_passes(self):
        """The usual layout: build symlink -> /usr/src/kernels/<ver>."""
        scenario = self.fixture_root / "valid-link"
        scenario.mkdir()
        tree = self.populated("valid-link")
        (scenario / "build").symlink_to(tree, target_is_directory=True)
        result = self.run_check_env("valid-link")
        self.assertIn(" -> ", result.stdout, result.stdout)
        self.assertIn(str(tree), result.stdout)
        self.assertNotIn("kernel-build-dir", result.stdout)

    def test_symlink_chain_resolves_to_populated_tree(self):
        """Chained symlinks resolve to the real tree and pass."""
        scenario = self.fixture_root / "chain"
        scenario.mkdir()
        tree = self.populated("chain")
        (scenario / "indirect").symlink_to(tree, target_is_directory=True)
        (scenario / "build").symlink_to(
            scenario / "indirect", target_is_directory=True
        )
        result = self.run_check_env("chain")
        self.assertIn(" -> ", result.stdout, result.stdout)
        self.assertIn(str(tree), result.stdout)
        self.assertNotIn("kernel-build-dir", result.stdout)

    def test_missing_build_directory_reported_missing(self):
        result = self.run_check_env("absent")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertNotIn(" -> ", result.stdout, result.stdout)
        self.assertIn("kernel-build-dir", result.stdout)


if __name__ == "__main__":
    unittest.main()
