# Copyright 2026 Alibaba Cloud
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Real-HTTP regression tests for the fixture download 416 completion branch.

Runs the embedded ``_FIXTURE_DOWNLOAD_SCRIPT`` from ``scripts/setup_env.py``
as an actual child process against a fixture-owned loopback HTTP server.
Covers the HTTP 416 (unsatisfied range) completion path: a nonempty existing
partial may only be promoted when a syntactically valid unsatisfied-range
``Content-Range`` header declares the complete byte length that the partial
already holds, and the 416 error response body must never be appended to
(or published as) the archive. Plain 200 and 206 downloads are controls.

No Hugging Face or other external network access occurs; every HTTP endpoint
in this module is a loopback server owned by the test itself.
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

ERROR_BODY = b"416 Requested Range Not Satisfiable: the file is complete already\n"


def _import_setup_env():
    """Import scripts/setup_env.py (stdlib-only at import time)."""
    spec = importlib.util.spec_from_file_location(
        "setup_env_416_test", str(REPO_ROOT / "scripts" / "setup_env.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _LoopbackServer:
    """Loopback HTTP server owned by the test, serving canned responses."""

    def __init__(self, status, content=b"", content_range=None, serve_range=False):
        handler = _make_handler(status, content, content_range, serve_range)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True
        )
        self._thread.start()
        self.url = (
            f"http://127.0.0.1:{self._httpd.server_address[1]}/fixtures.tar.gz"
        )

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=10)


def _make_handler(status, content, content_range, serve_range):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802 - http.server API
            if serve_range:
                range_header = self.headers.get("Range", "")
                start = 0
                if range_header.startswith("bytes="):
                    start = int(range_header[len("bytes="):].split("-")[0])
                chunk = content[start:]
                self.send_response(206)
                self.send_header(
                    "Content-Range",
                    f"bytes {start}-{len(content) - 1}/{len(content)}",
                )
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                self.wfile.write(chunk)
                return

            body = content if status == 200 else ERROR_BODY
            self.send_response(status)
            if content_range is not None:
                self.send_header("Content-Range", content_range)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence request logging
            pass

    return Handler


def _run_download(tmp_path, server_url):
    """Run the embedded fixture download script as a real child process."""
    setup_env = _import_setup_env()
    archive = tmp_path / "fixtures.tar.gz"
    script = setup_env._FIXTURE_DOWNLOAD_SCRIPT.format(
        url=server_url,
        archive=str(archive),
        hf_dataset="claw-eval/Claw-Eval",
    )
    return archive, subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _part_path(archive: Path) -> Path:
    return archive.with_suffix(archive.suffix + ".part")


def _make_tarball(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


TARBALL = _make_tarball(
    {
        "M001_clock/fixtures/config.json": b'{"tick": 1}\n',
        "M002_weather/fixtures/data.csv": b"city,temp\noslo,21\n",
    }
)


class Test416Completion:
    """The 416 branch must verify lengths and never touch the error body."""

    def test_416_matching_length_promotes_partial_verbatim(self, tmp_path):
        """Nonempty partial + valid ``bytes */N`` with N == size promotes as-is."""
        part = tmp_path / "fixtures.tar.gz.part"
        part.write_bytes(TARBALL)
        server = _LoopbackServer(
            416, content_range=f"bytes */{len(TARBALL)}"
        )
        try:
            archive, proc = _run_download(tmp_path, server.url)
        finally:
            server.close()
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert archive.read_bytes() == TARBALL
        assert not _part_path(archive).exists()

    def test_416_promoted_archive_is_valid_gzip_tarball(self, tmp_path):
        """The promoted archive remains a readable gzip tarball with members."""
        part = tmp_path / "fixtures.tar.gz.part"
        part.write_bytes(TARBALL)
        server = _LoopbackServer(
            416, content_range=f"bytes */{len(TARBALL)}"
        )
        try:
            archive, proc = _run_download(tmp_path, server.url)
        finally:
            server.close()
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert archive.stat().st_size == len(TARBALL)
        with tarfile.open(archive, "r:gz") as tf:
            assert sorted(tf.getnames()) == [
                "M001_clock/fixtures/config.json",
                "M002_weather/fixtures/data.csv",
            ]
            cfg = tf.extractfile("M001_clock/fixtures/config.json")
            assert cfg is not None and cfg.read() == b'{"tick": 1}\n'

    def test_416_uppercase_range_unit_promotes(self, tmp_path):
        """Range unit names are case-insensitive (RFC 9110 section 14.4)."""
        part = tmp_path / "fixtures.tar.gz.part"
        part.write_bytes(TARBALL)
        server = _LoopbackServer(
            416, content_range=f"BYTES */{len(TARBALL)}"
        )
        try:
            archive, proc = _run_download(tmp_path, server.url)
        finally:
            server.close()
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert archive.read_bytes() == TARBALL

    def test_416_missing_content_range_fails_and_preserves_partial(self, tmp_path):
        """416 without Content-Range must fail and keep the partial untouched."""
        part = tmp_path / "fixtures.tar.gz.part"
        part.write_bytes(TARBALL)
        server = _LoopbackServer(416, content_range=None)
        try:
            archive, proc = _run_download(tmp_path, server.url)
        finally:
            server.close()
        assert proc.returncode != 0
        assert not archive.exists()
        assert part.read_bytes() == TARBALL

    def test_416_malformed_content_range_fails(self, tmp_path):
        """Syntactically invalid unsatisfied-range lengths must not promote."""
        part = tmp_path / "fixtures.tar.gz.part"
        part.write_bytes(TARBALL)
        server = _LoopbackServer(416, content_range="bytes */not-a-length")
        try:
            archive, proc = _run_download(tmp_path, server.url)
        finally:
            server.close()
        assert proc.returncode != 0
        assert not archive.exists()
        assert part.read_bytes() == TARBALL

    def test_416_incomplete_partial_fails(self, tmp_path):
        """Partial shorter than the declared complete length must fail."""
        truncated = TARBALL[: len(TARBALL) // 2]
        part = tmp_path / "fixtures.tar.gz.part"
        part.write_bytes(truncated)
        server = _LoopbackServer(416, content_range=f"bytes */{len(TARBALL)}")
        try:
            archive, proc = _run_download(tmp_path, server.url)
        finally:
            server.close()
        assert proc.returncode != 0
        assert not archive.exists()
        assert part.read_bytes() == truncated

    def test_416_oversized_partial_fails(self, tmp_path):
        """Partial longer than the declared complete length must fail."""
        padded = TARBALL + b"\x00" * 128
        part = tmp_path / "fixtures.tar.gz.part"
        part.write_bytes(padded)
        server = _LoopbackServer(416, content_range=f"bytes */{len(TARBALL)}")
        try:
            archive, proc = _run_download(tmp_path, server.url)
        finally:
            server.close()
        assert proc.returncode != 0
        assert not archive.exists()
        assert part.read_bytes() == padded

    def test_416_fresh_download_never_publishes_error_body(self, tmp_path):
        """416 on a fresh request must fail without creating any archive."""
        server = _LoopbackServer(416, content_range=f"bytes */{len(TARBALL)}")
        try:
            archive, proc = _run_download(tmp_path, server.url)
        finally:
            server.close()
        assert proc.returncode != 0
        assert not archive.exists()
        part = _part_path(archive)
        assert not part.exists() or part.read_bytes() != ERROR_BODY

    def test_416_empty_partial_file_fails(self, tmp_path):
        """An empty existing .part is not a promotable complete partial."""
        part = tmp_path / "fixtures.tar.gz.part"
        part.write_bytes(b"")
        server = _LoopbackServer(416, content_range=f"bytes */{len(TARBALL)}")
        try:
            archive, proc = _run_download(tmp_path, server.url)
        finally:
            server.close()
        assert proc.returncode != 0
        assert not archive.exists()


class TestDownloadControls:
    """Plain 200/206 downloads keep working (baseline-passing controls)."""

    def test_200_full_download(self, tmp_path):
        server = _LoopbackServer(200, content=TARBALL, serve_range=False)
        try:
            archive, proc = _run_download(tmp_path, server.url)
        finally:
            server.close()
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert archive.read_bytes() == TARBALL

    def test_206_resume_download(self, tmp_path):
        offset = len(TARBALL) // 3
        part = tmp_path / "fixtures.tar.gz.part"
        part.write_bytes(TARBALL[:offset])
        server = _LoopbackServer(206, content=TARBALL, serve_range=True)
        try:
            archive, proc = _run_download(tmp_path, server.url)
        finally:
            server.close()
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert archive.read_bytes() == TARBALL
