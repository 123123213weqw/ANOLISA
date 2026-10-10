"""Global test fixtures for agent-sec-core."""

import os
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def v2_report_runtime(request, monkeypatch):
    """Provide an isolated V2 daemon to the unchanged V1 report cases."""
    report_tests = Path(__file__).parent / "e2e/cli/test_session_report_e2e.py"
    if os.environ.get("OBS_E2E_RUNTIME") != "v2" or request.node.path != report_tests:
        yield
        return

    # V1's pytest entry does not expose the tests namespace during collection.
    from tests.v2.e2e.conftest import (  # noqa: PLC0415
        _require,
        _start_daemon,
        _terminate,
    )

    request.getfixturevalue("isolated_data_dir")
    _require("agent-sec-cli")
    with tempfile.TemporaryDirectory(prefix="asc-report-", dir="/tmp") as directory:
        socket_path = Path(directory) / "daemon.sock"
        monkeypatch.setenv("AGENT_SEC_DAEMON_SOCKET", str(socket_path))
        process = _start_daemon(socket_path, [])
        try:
            yield
        finally:
            _terminate(process)
            assert process.returncode == 0
            assert not socket_path.exists()


def pytest_configure(config: pytest.Config) -> None:
    """Use a short basetemp on macOS to avoid AF_UNIX socket path length limit.

    macOS limits AF_UNIX socket paths to 104 bytes. pytest's default basetemp
    on macOS is under /private/var/folders/... which can exceed this limit.

    Placed at tests/ root so all subdirectories (unit-test, e2e, integration-test)
    benefit — tmp_path_factory in unit-test/conftest.py also produces short paths.

    /tmp/agd-pytest-<uid> is used instead of tempfile.gettempdir() because the
    latter returns /private/var/folders/... on macOS, which is exactly the long
    path we want to avoid. Residual directories are managed by pytest's built-in
    basetemp cleanup logic (keeps last 3 runs, removes older ones).
    """
    if sys.platform == "darwin" and not config.option.basetemp:
        basetemp = Path(f"/tmp/agd-pytest-{os.getuid()}")  # noqa: S108
        basetemp.mkdir(parents=True, exist_ok=True)
        basetemp.chmod(0o700)
        config.option.basetemp = basetemp
