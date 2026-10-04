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

"""Test agent execution logic.

Covers:
- Agent mode selection (gateway vs sandbox)
- Multi-modal attachment handling (M tasks)
- UserAgent conversation loop (C tasks)
- Service health checking before execution
- Container lifecycle for sandbox tasks

Task type coverage:
- T tasks: Gateway mode with mock services
- M tasks: Sandbox mode with Docker containers
- C tasks: Gateway mode with UserAgent multi-turn dialogue
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, call
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


class TestAgentModeSelection:
    """Test agent mode selection logic."""

    def test_gateway_mode_for_t_task(self, tmp_path):
        """T tasks use gateway mode."""
        from ce_runner._common import is_sandbox_task
        
        task_yaml = tmp_path / "task.yaml"
        task_yaml.write_text(
            "task_id: T001zh_email_triage\n"
            "services:\n"
            "  - name: gmail\n"
            "    port: 9100\n"
        )
        
        assert is_sandbox_task(str(task_yaml)) is False

    def test_sandbox_mode_for_m_task(self, tmp_path):
        """M tasks use sandbox mode."""
        from ce_runner._common import is_sandbox_task
        
        task_yaml = tmp_path / "task.yaml"
        task_yaml.write_text(
            "task_id: M101_chinese_food\n"
            "sandbox_files:\n"
            "  - /workspace/fixtures/media/image.jpg\n"
        )
        
        assert is_sandbox_task(str(task_yaml)) is True

    def test_gateway_mode_for_c_task(self, tmp_path):
        """C tasks use gateway mode (not sandbox)."""
        from ce_runner._common import is_sandbox_task
        
        task_yaml = tmp_path / "task.yaml"
        task_yaml.write_text(
            "task_id: C01zh_mortgage\n"
            "user_agent:\n"
            "  enabled: true\n"
            "  max_rounds: 8\n"
        )
        
        assert is_sandbox_task(str(task_yaml)) is False


class TestMultimodalAttachments:
    """Test multi-modal attachment handling (M tasks)."""

    def test_extract_attachments_from_prompt(self, tmp_path):
        """Extract attachment paths from M task prompt."""
        task_yaml = tmp_path / "task.yaml"
        task_yaml.write_text(
            "task_id: M101\n"
            "prompt:\n"
            "  text: 'Describe this image'\n"
            "  attachments:\n"
            "    - fixtures/media/image.jpg\n"
        )
        
        from ce_runner._common import load_task_yaml
        task = load_task_yaml(str(task_yaml))
        
        assert "prompt" in task
        assert "attachments" in task["prompt"]
        assert len(task["prompt"]["attachments"]) == 1

    def test_sandbox_files_declaration(self, tmp_path):
        """M tasks declare sandbox_files for container mount."""
        task_yaml = tmp_path / "task.yaml"
        task_yaml.write_text(
            "task_id: M099\n"
            "sandbox_files:\n"
            "  - fixtures/media/car.jpg\n"
            "  - /workspace/output.html\n"
        )
        
        from ce_runner._common import load_task_yaml
        task = load_task_yaml(str(task_yaml))
        
        assert "sandbox_files" in task
        assert len(task["sandbox_files"]) == 2


class TestUserAgentConversation:
    """Test UserAgent conversation loop (C tasks)."""

    def test_user_agent_config_extraction(self, tmp_path):
        """Extract user_agent configuration from C task."""
        task_yaml = tmp_path / "task.yaml"
        task_yaml.write_text(
            "task_id: C01zh_mortgage_prepay\n"
            "user_agent:\n"
            "  enabled: true\n"
            "  persona: '35岁上班族，有15万闲置资金'\n"
            "  max_rounds: 8\n"
        )
        
        from ce_runner._common import load_task_yaml
        task = load_task_yaml(str(task_yaml))
        
        assert "user_agent" in task
        assert task["user_agent"]["enabled"] is True
        assert task["user_agent"]["max_rounds"] == 8

    def test_user_agent_disabled_for_t_task(self, tmp_path):
        """T tasks don't have user_agent config."""
        task_yaml = tmp_path / "task.yaml"
        task_yaml.write_text(
            "task_id: T001\n"
            "services:\n"
            "  - name: gmail\n"
        )
        
        from ce_runner._common import load_task_yaml
        task = load_task_yaml(str(task_yaml))
        
        assert "user_agent" not in task


class TestServiceHealthCheck:
    """Test service health check before execution."""

    def test_service_list_extraction(self, tmp_path):
        """Extract service list from T task."""
        task_yaml = tmp_path / "task.yaml"
        task_yaml.write_text(
            "task_id: T009zh_contact_lookup\n"
            "services:\n"
            "  - name: contacts\n"
            "    port: 9103\n"
            "    health_check: http://localhost:9103/contacts/search\n"
        )
        
        from ce_runner._common import load_task_yaml
        task = load_task_yaml(str(task_yaml))
        
        assert "services" in task
        assert len(task["services"]) == 1
        assert task["services"][0]["name"] == "contacts"

    def test_no_services_for_m_task(self, tmp_path):
        """M tasks typically have no services."""
        task_yaml = tmp_path / "task.yaml"
        task_yaml.write_text(
            "task_id: M101\n"
            "services: []\n"
            "sandbox_files:\n"
            "  - /workspace/image.jpg\n"
        )
        
        from ce_runner._common import load_task_yaml
        task = load_task_yaml(str(task_yaml))
        
        assert task.get("services", []) == []


class TestTaskYamlLoading:
    """Test task.yaml loading and validation."""

    def test_load_real_t_task(self):
        """Load real T task yaml structure."""
        from ce_runner._common import load_task_yaml
        
        # Use actual task if available, otherwise skip
        task_path = Path("claw-eval/tasks/T001zh_email_triage/task.yaml")
        if task_path.exists():
            task = load_task_yaml(str(task_path))
            assert task["task_id"] == "T001zh_email_triage"
            assert "services" in task

    def test_load_real_m_task(self):
        """Load real M task yaml structure."""
        from ce_runner._common import load_task_yaml
        
        task_path = Path("claw-eval/tasks/M101_chinese_food_identification_zh/task.yaml")
        if task_path.exists():
            task = load_task_yaml(str(task_path))
            assert task["task_id"] == "M101_chinese_food_identification_zh"
            assert "sandbox_files" in task or "attachments" in task.get("prompt", {})

    def test_load_real_c_task(self):
        """Load real C task yaml structure."""
        from ce_runner._common import load_task_yaml
        
        task_path = Path("claw-eval/tasks/C01zh_mortgage_prepay/task.yaml")
        if task_path.exists():
            task = load_task_yaml(str(task_path))
            assert task["task_id"] == "C01zh_mortgage_prepay"
            assert "user_agent" in task


class TestTextAttachmentRouting:
    """Pure-text attachments must NOT trigger the multimodal HTTP path.

    Regression: T091/T096 ship .txt fixtures via prompt.attachments. The
    previous implementation blindly POSTed every attachment as image_url
    (with image/jpeg fallback when MIME guess failed), causing Dashscope
    400s and empty session files.
    """

    def _write_task(self, tmp_path: Path, attachment_names: list[str],
                    create_files: bool = True,
                    body: str = "ATTACHMENT_BODY_MARKER") -> Path:
        for name in attachment_names:
            if create_files:
                (tmp_path / name).write_text(body, encoding="utf-8")
        attachments_yaml = "\n".join(
            f"    - {name}" for name in attachment_names
        )
        task_yaml = tmp_path / "task.yaml"
        task_yaml.write_text(
            "task_id: T091_text_attachment\n"
            "prompt:\n"
            "  text: 'Summarize the file'\n"
            "  attachments:\n"
            f"{attachments_yaml}\n"
        )
        return task_yaml

    def test_text_attachments_use_cli_not_http(self, tmp_path):
        """Pure .txt attachments go through CLI subprocess.Popen with --message."""
        from ce_runner import agent as agent_mod

        task_yaml = self._write_task(tmp_path, ["doc.txt"])

        fake_proc = MagicMock()
        fake_proc.pid = 12345
        fake_proc.communicate.return_value = (b"", b"")

        with patch.object(agent_mod, "httpx") as mock_httpx, \
             patch.object(agent_mod.subprocess, "Popen",
                          return_value=fake_proc) as mock_popen, \
             patch.object(agent_mod, "_find_session_file",
                          return_value="/tmp/fake.jsonl"), \
             patch.object(agent_mod, "_extract_session_id_from_result",
                          return_value=""), \
             patch.object(agent_mod.os, "killpg"), \
             patch.object(agent_mod.os, "getpgid", return_value=12345):
            session_file = agent_mod.run_agent(
                session_id="sess-test-1",
                task_yaml=str(task_yaml),
                timeout=10,
                agent_id=None,
            )

        assert session_file == "/tmp/fake.jsonl"
        # No HTTP call should have been made for a pure-text attachment.
        assert not mock_httpx.post.called, (
            "httpx.post must NOT be invoked for pure-text attachments"
        )
        # CLI was invoked.
        assert mock_popen.called, "subprocess.Popen must be invoked"
        cmd = mock_popen.call_args[0][0]
        assert "--message" in cmd, f"--message missing in cmd: {cmd}"

        # The injected message must contain attachment basename and body.
        msg_idx = cmd.index("--message") + 1
        injected = cmd[msg_idx]
        assert "doc.txt" in injected, f"basename missing in message: {injected!r}"
        assert "ATTACHMENT_BODY_MARKER" in injected, (
            f"attachment body missing in message: {injected!r}"
        )

    def test_missing_text_attachment_is_skipped_not_raised(self, tmp_path):
        """A non-existent text attachment must not crash run_agent."""
        from ce_runner import agent as agent_mod

        task_yaml = self._write_task(
            tmp_path, ["nope.txt"], create_files=False,
        )

        fake_proc = MagicMock()
        fake_proc.pid = 12346
        fake_proc.communicate.return_value = (b"", b"")

        with patch.object(agent_mod, "httpx") as mock_httpx, \
             patch.object(agent_mod.subprocess, "Popen",
                          return_value=fake_proc) as mock_popen, \
             patch.object(agent_mod, "_find_session_file",
                          return_value=""), \
             patch.object(agent_mod, "_extract_session_id_from_result",
                          return_value=""), \
             patch.object(agent_mod.os, "killpg"), \
             patch.object(agent_mod.os, "getpgid", return_value=12346):
            # Must not raise.
            agent_mod.run_agent(
                session_id="sess-test-2",
                task_yaml=str(task_yaml),
                timeout=10,
                agent_id=None,
            )

        assert not mock_httpx.post.called
        assert mock_popen.called


class TestApiDeadlineBudget:
    """``timeout`` must cap the total wall-clock of all HTTP attempts.

    Regression: the deadline was only checked *between* attempts while each
    attempt's read timeout followed the inactivity ladder (120/240/480/960s
    by default) regardless of the remaining budget, so e.g. ``--timeout 60``
    let a single never-responding attempt read for 120s (2x budget), and a
    late attempt up to 960s (16x).
    """

    def _write_config(self, tmp_path, port):
        cfg = tmp_path / "openclaw.json"
        cfg.write_text(json.dumps({
            "gateway": {"port": port, "auth": {"token": "test-token"}}}))
        return cfg

    @staticmethod
    def _clear_proxies(monkeypatch):
        for var in ("http_proxy", "https_proxy", "all_proxy",
                    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    def test_deadline_bounds_wall_clock(self, tmp_path, monkeypatch):
        """A never-responding server must not push wall past the budget."""
        import socket
        import threading
        import time as _time
        from ce_runner import agent as agent_mod

        self._clear_proxies(monkeypatch)
        # Keep the pristine overshould bounded so the test fails fast:
        # inactivity 6s means an unclamped first attempt reads for ~6s.
        monkeypatch.setenv("CE_RUNNER_HTTP_INACTIVITY_S", "6")

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        conns = []
        stop = threading.Event()

        def acceptor():
            listener.settimeout(0.2)
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                    conns.append(conn)
                except socket.timeout:
                    continue
                except OSError:
                    break

        threading.Thread(target=acceptor, daemon=True).start()
        try:
            port = listener.getsockname()[1]
            cfg = self._write_config(tmp_path, port)
            monkeypatch.setattr(agent_mod, "OPENCLAW_CONFIG", str(cfg))

            t0 = _time.monotonic()
            result = agent_mod._run_first_turn_via_api(
                "hi", [], [], timeout=2, agent_id=None,
                task_dir=str(tmp_path))
            wall = _time.monotonic() - t0
        finally:
            stop.set()
            for conn in conns:
                try:
                    conn.close()
                except OSError:
                    pass
            listener.close()

        assert result == ""
        assert wall <= 3.5, (
            f"wall={wall:.1f}s exceeded 2s budget (+1.5s epsilon): the "
            f"in-flight attempt carried its full read timeout past the "
            f"deadline"
        )
        assert "http_timeout_after" in agent_mod.last_agent_error()

    def test_fast_response_unchanged(self, tmp_path, monkeypatch, capsys):
        """A fast 200 response completes without retry or budget interference."""
        import threading
        import time as _time
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from ce_runner import agent as agent_mod

        self._clear_proxies(monkeypatch)
        monkeypatch.delenv("CE_RUNNER_HTTP_INACTIVITY_S", raising=False)

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            cfg = self._write_config(tmp_path, server.server_address[1])
            sessions_dir = tmp_path / "sessions"
            sessions_dir.mkdir()
            session_file = sessions_dir / "sess.jsonl"
            session_file.write_text('{"type": "message"}\n')
            monkeypatch.setattr(agent_mod, "OPENCLAW_CONFIG", str(cfg))
            monkeypatch.setattr(agent_mod, "SESSIONS_DIR", str(sessions_dir))

            t0 = _time.monotonic()
            result = agent_mod._run_first_turn_via_api(
                "hi", [], [], timeout=30, agent_id=None,
                task_dir=str(tmp_path))
            wall = _time.monotonic() - t0
        finally:
            server.shutdown()

        assert result == str(session_file)
        assert wall < 10, f"fast response took {wall:.1f}s"
        assert "[RETRY]" not in capsys.readouterr().out

    def test_ladder_still_escalates_when_budget_allows(self, tmp_path,
                                                       monkeypatch):
        """Per-attempt clamping must not flatten the retry ladder itself."""
        from ce_runner import agent as agent_mod
        import httpx

        self._clear_proxies(monkeypatch)
        monkeypatch.setenv("CE_RUNNER_HTTP_INACTIVITY_S", "10")
        monkeypatch.setenv("CE_RUNNER_HTTP_MAX_RETRIES", "3")

        reads = []

        class FakeStream:
            def __enter__(self):
                raise httpx.ReadTimeout("stall")

            def __exit__(self, *args):
                return False

        class FakeClient:
            def __init__(self, timeout=None):
                self.timeout = timeout

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def stream(self, *args, **kwargs):
                reads.append(self.timeout.read)
                return FakeStream()

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        cfg = self._write_config(tmp_path, 18789)
        with patch.object(agent_mod.httpx, "Client", FakeClient), \
             patch.object(agent_mod.time, "sleep", lambda *_: None), \
             patch.object(agent_mod, "OPENCLAW_CONFIG", str(cfg)), \
             patch.object(agent_mod, "SESSIONS_DIR", str(sessions_dir)):
            result = agent_mod._run_first_turn_via_api(
                "hi", [], [], timeout=10000, agent_id=None,
                task_dir=str(tmp_path))

        assert result == ""
        # inactivity=10 ladder: attempts 1-3 at 10s, attempt 4 at 20s.
        assert reads == [10.0, 10.0, 10.0, 20.0], (
            f"read-timeout ladder flattened: {reads}"
        )
