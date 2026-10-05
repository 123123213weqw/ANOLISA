"""Download upstream kernels from the archive family of the resolved major."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "src/os-skills/devops/kernel-dev/scripts/build-kernel.sh"
)


@unittest.skipUnless(os.name == "posix", "drives the bash script with function stubs")
class KernelArchiveFamilyTests(unittest.TestCase):
    """build_upstream must derive v<major>.x from the resolved version, not v6.x."""

    def run_build_upstream(self, version, latest_page=""):
        """Run build_upstream with wget/tar/make stubbed; no downloads or builds."""
        source = SCRIPT.read_text(encoding="utf-8")
        # Keep the script up to build_upstream; drop install/status/main.
        source = source.split("# Install compiled kernel")[0]
        with tempfile.TemporaryDirectory() as work:
            stubs = "\n".join(
                [
                    'wget() { printf \'DOWNLOAD %s\\n\' "$2"; : > "$WORK_DIR/${2##*/}"; }',
                    'tar() { printf \'EXTRACT %s\\n\' "$2"; mkdir -p "${2%.tar.xz}"; }',
                    "make() { printf 'MAKE %s\\n' \"$*\"; }",
                    "curl() { printf '%s' \"$FAKE_KERNEL_PAGE\"; }",
                    f'WORK_DIR="{work}"',
                    f'LOG_FILE="{work}/build.log"',
                    "PARALLEL_JOBS=2",
                    "CONFIG_TYPE=defconfig",
                    "build_upstream",
                ]
            )
            env = {**os.environ, "FAKE_KERNEL_PAGE": latest_page}
            return subprocess.run(
                [
                    "bash",
                    "-c",
                    source + "\n" + stubs + "\n",
                    str(SCRIPT),
                    "upstream",
                    version,
                    "2",
                    "defconfig",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )

    def download_url(self, result):
        for line in result.stdout.splitlines():
            if line.startswith("DOWNLOAD "):
                return line[len("DOWNLOAD ") :]
        return ""

    def assert_family(self, version, family, latest_page=""):
        result = self.run_build_upstream(version, latest_page)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        url = self.download_url(result)
        self.assertEqual(
            url,
            f"https://cdn.kernel.org/pub/linux/kernel/{family}/linux-{version}.tar.xz",
            result.stdout,
        )

    def test_explicit_v4_version_downloads_from_v4_archive(self):
        self.assert_family("4.19.90", "v4.x")

    def test_explicit_v5_version_downloads_from_v5_archive(self):
        self.assert_family("5.15.148", "v5.x")

    def test_explicit_v7_version_downloads_from_v7_archive(self):
        self.assert_family("7.1.2", "v7.x")

    def test_latest_resolving_to_v7_downloads_from_v7_archive(self):
        page = 'href="linux-7.2.1.tar.xz">linux-7.2.1.tar.xz</a>'
        self.assert_family("7.2.1", "v7.x", latest_page=page)

    def test_v6_control_keeps_v6_archive(self):
        self.assert_family("6.12.9", "v6.x")


if __name__ == "__main__":
    unittest.main()
