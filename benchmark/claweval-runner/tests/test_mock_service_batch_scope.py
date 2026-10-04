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

"""Test runner-scoped mock-service cleanup.

``cleanup_mock_services`` used to SIGKILL every host process whose argv
mentioned "mock_services" with no notion of ownership: two concurrent
ce-runner batches on one shared host killed each other's live services
mid-trial. Services spawned by this runner are now tagged with a per-batch
id (env ``CE_RUNNER_BATCH_ID``) and tracked in a pid registry; cleanup only
touches this batch's pids. Unmarked legacy processes are swept only by the
explicit ``include_unmarked`` orphan mode.

Real-process tests spawn ONLY their own fake sleepers (named
``mock_services_sleeper.py`` so they match the legacy ps filter) and kill
exactly those pids in cleanup — no other process is touched.
"""

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

SLEEPER_SRC = '''
import time
time.sleep(300)
'''


@pytest.fixture(autouse=True)
def _clean_pid_registry():
    """Discard cross-test-module state from the batch-scope machinery.

    Two pieces of module state outlive earlier test modules:

    * the pid registry — earlier modules that patch ``subprocess.Popen``
      with MagicMocks (e.g. the dead-service boot tests) register
      MagicMock "pids" that later break
      ``sorted(registered_mock_service_pids())`` in ``cleanup_mock_services``;
    * the cached batch id — ``mock_services_batch_id`` mints and caches a
      pid-derived fallback id on first use, so an earlier module that
      spawns without ``CE_RUNNER_BATCH_ID`` pins the fallback for the rest
      of the process and hides the env var these tests set.

    Reset both around every test here so each observes its own env.
    """
    from ce_runner.infra import forget_mock_service_pids
    from ce_runner import parallel as _parallel

    forget_mock_service_pids()
    _parallel._batch_scope_id = None
    yield
    forget_mock_service_pids()
    _parallel._batch_scope_id = None


@contextlib.contextmanager
def _spawn_sleeper(tmp_path, batch_id):
    """Spawn a fake mock-service process; yield the Popen; always kill it."""
    sleeper = tmp_path / "mock_services_sleeper.py"
    if not sleeper.exists():
        sleeper.write_text(SLEEPER_SRC)
    env = dict(os.environ)
    env.pop("CE_RUNNER_BATCH_ID", None)
    if batch_id is not None:
        env["CE_RUNNER_BATCH_ID"] = batch_id
    proc = subprocess.Popen(
        [sys.executable, str(sleeper)], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    time.sleep(0.5)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def _rlimit_ctx():
    """macOS rejects setrlimit(RLIMIT_AS) in preexec_fn; skip it there."""
    if sys.platform == "darwin":
        return patch("ce_runner.parallel.resource.setrlimit",
                     lambda *a, **k: None)
    return contextlib.nullcontext()


def _write_service_task(tmp_path, sleeper):
    task_dir = tmp_path / "T001_scope"
    task_dir.mkdir(exist_ok=True)
    task_yaml = task_dir / "task.yaml"
    task_yaml.write_text(
        "task_id: T001_scope\n"
        "services:\n"
        "  - name: sleeper\n"
        "    port: 1\n"
        f"    command: {sys.executable} {sleeper}\n"
        "    health_check: \"\"\n"
        "    ready_timeout: 0\n")
    return str(task_yaml), str(task_dir)


def _cleanup_registered():
    from ce_runner.parallel import (forget_mock_service_pids,
                                    registered_mock_service_pids)
    for pid in registered_mock_service_pids():
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)
    forget_mock_service_pids()


# ── Real-process scoping tests ────────────────────────────────────────


class TestBatchScopedCleanup:
    """cleanup_mock_services kills only THIS batch's services."""

    def test_foreign_batch_service_survives_cleanup(self, tmp_path):
        """A live service tagged with a DIFFERENT CE_RUNNER_BATCH_ID (i.e.
        another concurrent ce-runner batch on this host) must survive our
        chunk-boundary cleanup."""
        from ce_runner.infra import cleanup_mock_services

        with _spawn_sleeper(tmp_path, batch_id="other-ce-runner-batch") as proc:
            foreign_pid = proc.pid
            os.environ["CE_RUNNER_BATCH_ID"] = "my-ce-runner-batch"
            try:
                cleanup_mock_services()
            finally:
                os.environ.pop("CE_RUNNER_BATCH_ID", None)
            time.sleep(0.5)
            assert proc.poll() is None, (
                f"foreign batch service (pid {foreign_pid}) was killed by "
                "cleanup_mock_services — cross-batch kill")

    def test_own_batch_service_killed_by_cleanup(self, tmp_path):
        """A service this runner spawned via start_mock_services_with_offset
        (tagged with our batch id, registered) is killed by our cleanup."""
        from ce_runner.infra import cleanup_mock_services
        from ce_runner.parallel import start_mock_services_with_offset

        sleeper = tmp_path / "mock_services_sleeper.py"
        sleeper.write_text(SLEEPER_SRC)
        task_yaml, task_dir = _write_service_task(tmp_path, sleeper)
        os.environ["CE_RUNNER_BATCH_ID"] = "my-ce-runner-batch"
        try:
            with _rlimit_ctx():
                start_mock_services_with_offset(task_yaml, task_dir,
                                                port_offset=0)
            alive = subprocess.run(
                ["pgrep", "-f", "mock_services_sleeper.py"],
                capture_output=True, text=True).stdout.split()
            assert len(alive) == 1, f"expected 1 sleeper, got {alive}"

            cleanup_mock_services()

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                alive = subprocess.run(
                    ["pgrep", "-f", "mock_services_sleeper.py"],
                    capture_output=True, text=True).stdout.split()
                if not alive:
                    break
                time.sleep(0.2)
            assert not alive, (
                f"own-batch service pids {alive} survived cleanup")
        finally:
            os.environ.pop("CE_RUNNER_BATCH_ID", None)
            _cleanup_registered()

    def test_spawned_service_carries_batch_id_env(self, tmp_path):
        """Services spawned through the production path are tagged with the
        runner's batch id via the CE_RUNNER_BATCH_ID env var."""
        from ce_runner.parallel import (start_mock_services_with_offset,
                                        registered_mock_service_pids)

        sleeper = tmp_path / "env_probe_service.py"
        marker_file = tmp_path / "batch_id.txt"
        sleeper.write_text(
            "import os, time\n"
            "open(r'%s', 'w').write(os.environ.get('CE_RUNNER_BATCH_ID', "
            "'<unset>'))\n"
            "time.sleep(300)\n" % str(marker_file))
        task_dir = tmp_path / "T002_env"
        task_dir.mkdir(exist_ok=True)
        task_yaml = task_dir / "task.yaml"
        task_yaml.write_text(
            "task_id: T002_env\n"
            "services:\n"
            "  - name: probe\n"
            "    port: 1\n"
            f"    command: {sys.executable} {sleeper}\n"
            "    health_check: \"\"\n"
            "    ready_timeout: 0\n")

        os.environ["CE_RUNNER_BATCH_ID"] = "my-ce-runner-batch"
        try:
            with _rlimit_ctx():
                start_mock_services_with_offset(str(task_yaml),
                                                str(task_dir), port_offset=0)
            assert registered_mock_service_pids(), "service pid not registered"
            deadline = time.monotonic() + 5
            value = None
            while time.monotonic() < deadline:
                if marker_file.exists():
                    value = marker_file.read_text()
                    break
                time.sleep(0.1)
            assert value == "my-ce-runner-batch", (
                f"spawned service env tag = {value!r}")
        finally:
            os.environ.pop("CE_RUNNER_BATCH_ID", None)
            _cleanup_registered()


class TestUnmarkedPolicy:
    """Unmarked legacy services are swept only in explicit orphan mode."""

    def test_unmarked_left_alone_by_default(self, tmp_path):
        """A pre-scoping/legacy service (no CE_RUNNER_BATCH_ID) is not
        killed by the regular per-chunk cleanup."""
        from ce_runner.infra import cleanup_mock_services

        with _spawn_sleeper(tmp_path, batch_id=None) as proc:
            cleanup_mock_services()
            time.sleep(0.5)
            assert proc.poll() is None, (
                "unmarked service killed by default cleanup")

    def test_unmarked_killed_in_explicit_orphan_sweep(self, tmp_path):
        """include_unmarked=True (explicit orphan sweep) still kills legacy
        unmarked mock_services processes."""
        from ce_runner.infra import cleanup_mock_services

        with _spawn_sleeper(tmp_path, batch_id=None) as proc:
            cleanup_mock_services(include_unmarked=True)
            deadline = time.monotonic() + 5
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.1)
            assert proc.poll() is not None, (
                "unmarked service survived explicit orphan sweep")


# ── Unit tests (no real processes) ────────────────────────────────────


class TestBatchIdMinting:
    """mock_services_batch_id: env override wins, else per-pid id."""

    def test_env_override(self):
        import ce_runner.parallel as parallel

        old = parallel._batch_scope_id
        old_env = os.environ.get("CE_RUNNER_BATCH_ID")
        try:
            parallel._batch_scope_id = None
            os.environ["CE_RUNNER_BATCH_ID"] = "explicit-batch"
            assert parallel.mock_services_batch_id() == "explicit-batch"
        finally:
            parallel._batch_scope_id = old
            if old_env is None:
                os.environ.pop("CE_RUNNER_BATCH_ID", None)
            else:
                os.environ["CE_RUNNER_BATCH_ID"] = old_env

    def test_pid_fallback_is_stable(self):
        import ce_runner.parallel as parallel

        old = parallel._batch_scope_id
        old_env = os.environ.pop("CE_RUNNER_BATCH_ID", None)
        try:
            parallel._batch_scope_id = None
            first = parallel.mock_services_batch_id()
            second = parallel.mock_services_batch_id()
            assert first == second
            assert first == f"ce-runner-{os.getpid()}"
        finally:
            parallel._batch_scope_id = old
            if old_env is not None:
                os.environ["CE_RUNNER_BATCH_ID"] = old_env


class TestUnmarkedSweepUnit:
    """The explicit orphan sweep parses ps output (mocked, no processes)."""

    def test_sweep_kills_mock_services_rows(self):
        from ce_runner.infra import cleanup_mock_services

        result = MagicMock()
        result.stdout = (
            "USER   PID  COMMAND\n"
            f"u 111 {sys.executable} somewhere/mock_services/gmail.py\n"
            f"u 222 {sys.executable} unrelated.py\n"
        )
        with patch("ce_runner.infra.subprocess.run", return_value=result), \
             patch("ce_runner.infra.os.getpid", return_value=99999), \
             patch("ce_runner.infra.os.kill") as mock_kill:
            cleanup_mock_services(include_unmarked=True)

        mock_kill.assert_called_once_with(111, signal.SIGKILL)

    def test_sweep_skips_garbage_and_own_pid(self):
        from ce_runner.infra import cleanup_mock_services

        result = MagicMock()
        result.stdout = (
            "USER   PID  COMMAND\n"
            "u notapid mock_services/web.py\n"
            "u 424242 this-runner-mock_services\n"
        )
        with patch("ce_runner.infra.subprocess.run", return_value=result), \
             patch("ce_runner.infra.os.getpid", return_value=424242), \
             patch("ce_runner.infra.os.kill") as mock_kill:
            cleanup_mock_services(include_unmarked=True)

        mock_kill.assert_not_called()
