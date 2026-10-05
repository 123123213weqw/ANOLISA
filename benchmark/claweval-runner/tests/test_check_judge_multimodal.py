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

"""Tests for scripts/check_judge_multimodal.py.

Covers:
- check_multimodal returns True only for the instructed single-word
  color reply; wrong colors, inability statements and prose fail
- check_multimodal returns False on NO_IMAGE / empty / None responses
- Embedded probe PNG is valid, 1x1 and an opaque red pixel
- CLI exit/PASS/FAIL paths with the captured request payload
"""
from __future__ import annotations

import base64
import struct
import sys
import types
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_judge_multimodal import TINY_RED_PNG_B64, check_multimodal  # noqa: E402
import check_judge_multimodal  # noqa: E402


def decode_probe_png() -> tuple[bytes, bytes]:
    """Fully decode the embedded PNG: verify signature and chunk CRCs,
    then return (ihdr, decompressed scanline) so callers can assert the
    actual pixel, not just the container."""
    data = base64.b64decode(TINY_RED_PNG_B64)
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a valid PNG signature"
    off = 8
    idat = b""
    while off < len(data):
        (length,) = struct.unpack(">I", data[off:off + 4])
        ctype = data[off + 4:off + 8]
        payload = data[off + 8:off + 8 + length]
        (crc,) = struct.unpack(">I", data[off + 8 + length:off + 12 + length])
        assert zlib.crc32(ctype + payload) & 0xFFFFFFFF == crc, f"bad CRC in {ctype!r}"
        if ctype == b"IHDR":
            ihdr = payload
        elif ctype == b"IDAT":
            idat += payload
        off += 12 + length
    return ihdr, zlib.decompress(idat)


class TestCheckMultimodal:
    """Unit tests for check_multimodal using mocked OpenAI client."""

    @staticmethod
    def _mock_client(response_text: str):
        """Build a mock client factory that returns *response_text*."""
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = response_text

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_resp

        def factory(model, base_url, api_key):
            return mock_client

        return factory, mock_client

    # ── Success cases ──────────────────────────────────────────────

    @pytest.mark.parametrize("response_text", [
        "red",
        "Red",
        "RED",
        "  red  ",
    ])
    def test_model_sees_image(self, response_text):
        """Model returns the instructed single-word color -> vision capable."""
        factory, mock_client = self._mock_client(response_text)
        assert check_multimodal("test-model", "http://x", "k", factory) is True

    # ── Failure cases ──────────────────────────────────────────────

    def test_model_reports_no_image(self):
        """Model explicitly reports NO_IMAGE -> not vision capable."""
        factory, _ = self._mock_client("NO_IMAGE")
        assert check_multimodal("test-model", "http://x", "k", factory) is False

    def test_model_reports_no_image_lowercase(self):
        """Case-insensitive NO_IMAGE detection."""
        factory, _ = self._mock_client("no_image because I'm text-only")
        assert check_multimodal("test-model", "http://x", "k", factory) is False

    def test_empty_response(self):
        """Empty response -> not vision capable."""
        factory, _ = self._mock_client("")
        assert check_multimodal("test-model", "http://x", "k", factory) is False

    def test_none_response(self):
        """None response (content is None) -> not vision capable."""
        factory, _ = self._mock_client(None)
        assert check_multimodal("test-model", "http://x", "k", factory) is False

    def test_wrong_color_rejected(self):
        """A confident but wrong color is not evidence of seeing the fixture."""
        factory, _ = self._mock_client("blue")
        assert check_multimodal("test-model", "http://x", "k", factory) is False

    def test_unrelated_ack_rejected(self):
        """An unrelated 'OK' reply is not evidence of seeing the fixture."""
        factory, _ = self._mock_client("OK")
        assert check_multimodal("test-model", "http://x", "k", factory) is False

    def test_explicit_inability_rejected(self):
        """'I cannot see images' is an inability statement, not a color."""
        factory, _ = self._mock_client("I cannot see images")
        assert check_multimodal("test-model", "http://x", "k", factory) is False

    @pytest.mark.parametrize("response_text", [
        "the color is red",
        "I see red",
    ])
    def test_out_of_protocol_prose_rejected(self, response_text):
        """The prompt asks for the single word only; prose is out of protocol."""
        factory, _ = self._mock_client(response_text)
        assert check_multimodal("test-model", "http://x", "k", factory) is False

    # ── Edge cases ─────────────────────────────────────────────────

    def test_whitespace_only_response(self):
        """Whitespace-only response -> not vision capable."""
        factory, _ = self._mock_client("   \n  ")
        assert check_multimodal("test-model", "http://x", "k", factory) is False


class TestProbeImage:
    """Verify the embedded probe image is a valid opaque red 1x1 PNG."""

    def test_image_is_valid_base64(self):
        import base64
        data = base64.b64decode(TINY_RED_PNG_B64)
        # PNG magic bytes
        assert data[:8] == b'\x89PNG\r\n\x1a\n', "Embedded probe image is not a valid PNG"

    def test_image_is_1x1(self):
        data = base64.b64decode(TINY_RED_PNG_B64)
        # IHDR starts at byte 16 (8-byte signature + 4-byte length + 4-byte 'IHDR')
        width, height = struct.unpack('>II', data[16:24])
        assert width == 1
        assert height == 1

    def test_image_pixel_is_opaque_red(self):
        """The sole RGBA pixel must decode to FF 00 00 FF (opaque red)."""
        ihdr, scanline = decode_probe_png()
        assert ihdr[8] == 8 and ihdr[9] == 6, "must be 8-bit RGBA"
        assert scanline == b"\x00\xff\x00\x00\xff", \
            f"probe pixel is {scanline[1:].hex()}, not opaque red ff0000ff"

    def test_image_alpha_fully_opaque(self):
        """A color verdict requires a fully opaque fixture pixel."""
        _, scanline = decode_probe_png()
        assert scanline[-1] == 0xFF, "probe pixel alpha must be 0xFF"


class TestCliPaths:
    """Actual CLI exit/PASS/FAIL paths with the captured request payload."""

    @staticmethod
    def _install_fake_openai(monkeypatch, response_text):
        """Install a fake ``openai`` module whose client records the
        request payload and replies with *response_text*; main() imports
        OpenAI lazily, so it picks the fake up without touching the wire."""
        captured: dict = {}

        class _Completions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(choices=[
                    SimpleNamespace(message=SimpleNamespace(content=response_text)),
                ])

        class _Client:
            def __init__(self, api_key=None, base_url=None):
                self.chat = SimpleNamespace(completions=_Completions())

        fake = types.ModuleType("openai")
        fake.OpenAI = _Client
        monkeypatch.setitem(sys.modules, "openai", fake)
        return captured

    def _run_cli(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "check_judge_multimodal.py",
            "--model", "test-model",
            "--base-url", "http://x",
            "--api-key", "k",
        ])
        with pytest.raises(SystemExit) as exc:
            check_judge_multimodal.main()
        return exc.value.code, capsys.readouterr().out

    def test_cli_pass_on_red_with_red_payload(self, monkeypatch, capsys):
        """Correctly spelled single-word reply -> exit 0, PASS, and the
        request payload carries the opaque red fixture."""
        captured = self._install_fake_openai(monkeypatch, "red")
        code, out = self._run_cli(monkeypatch, capsys)

        assert code == 0
        assert "PASS" in out
        content = captured["messages"][0]["content"]
        image_part = next(p for p in content if p["type"] == "image_url")
        assert image_part["image_url"]["url"] == \
            f"data:image/png;base64,{TINY_RED_PNG_B64}"
        # The payload fixture must be the decoded opaque red pixel, not
        # merely whatever constant the module ships.
        ihdr, scanline = decode_probe_png()
        assert scanline == b"\x00\xff\x00\x00\xff"

    def test_cli_fail_on_wrong_color(self, monkeypatch, capsys):
        """A wrong color reply -> exit 1, FAIL."""
        self._install_fake_openai(monkeypatch, "blue")
        code, out = self._run_cli(monkeypatch, capsys)
        assert code == 1
        assert "FAIL" in out

    def test_cli_fail_on_no_image(self, monkeypatch, capsys):
        """NO_IMAGE reply -> exit 1, FAIL (control)."""
        self._install_fake_openai(monkeypatch, "NO_IMAGE")
        code, out = self._run_cli(monkeypatch, capsys)
        assert code == 1
        assert "FAIL" in out

