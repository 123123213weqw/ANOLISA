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

"""_probe_exec readiness contract: exact echo marker, error flags, bounded retries.

Parameterized probe-loop cases with HTTP and sleep mocked -- no network,
Docker, or model execution. Each case inspects the actual retry count,
payload, timeout and backoff schedule.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ce_runner import sandbox_helpers  # noqa: E402

URL = "http://localhost:9101"
ENDPOINT = f"{URL}/exec"
PAYLOAD = {"command": "echo ok", "timeout_seconds": 5}
DEFAULT_BACKOFF = [1, 2, 4, 4]

OK = (200, {"stdout": "ok"})


class _FakeHttpx:
    """Replay a scripted response list, repeating the final entry."""

    def __init__(self, script):
        self.calls = []
        self._script = script

    def post(self, endpoint, json=None, timeout=None):
        self.calls.append(
            {"endpoint": endpoint, "json": json, "timeout": timeout}
        )
        step = self._script[min(len(self.calls) - 1, len(self._script) - 1)]
        if step == "exc":
            raise ConnectionError("probe connection refused")
        status, body = step
        if body == "raise":

            def _no_json():
                raise ValueError("non-JSON body")

            return SimpleNamespace(status_code=status, json=_no_json)

        return SimpleNamespace(
            status_code=status, json=lambda _b=body: _b
        )


def _harness(monkeypatch, script):
    """Patch httpx/time in sandbox_helpers; return the call and sleep recorders."""
    fake_http = _FakeHttpx(script)
    sleeps = []
    monkeypatch.setattr(sandbox_helpers, "httpx", fake_http)
    monkeypatch.setattr(
        sandbox_helpers,
        "time",
        SimpleNamespace(sleep=sleeps.append),
    )
    return fake_http, sleeps


# (case, script, max_attempts, expect_ready, expected_posts, expected_sleeps)
CASES = [
    # -- controls: readiness evidence that must be accepted -----------------
    ("exact_marker", [OK], 5, True, 1, []),
    ("padded_marker", [(200, {"stdout": "  ok  "})], 5, True, 1, []),
    (
        "marker_after_two_failures",
        [(500, OK), (200, {"stdout": "nope"}), OK],
        5,
        True,
        3,
        [1, 2],
    ),
    (
        "sdk_exit_code_not_inferred",
        [(200, {"stdout": "ok", "exit_code": 1, "duration_ms": 3})],
        5,
        True,
        1,
        [],
    ),
    # -- controls: malformed responses that must stay rejected --------------
    (
        "bare_list_body",
        [(200, ["ok"])],
        5,
        False,
        5,
        DEFAULT_BACKOFF,
    ),
    (
        "missing_stdout",
        [(200, {"stderr": "quiet"})],
        5,
        False,
        5,
        DEFAULT_BACKOFF,
    ),
    (
        "http_500_with_marker_body",
        [(500, {"stdout": "ok"})],
        5,
        False,
        5,
        DEFAULT_BACKOFF,
    ),
    (
        "non_json_body",
        [(200, "raise")],
        5,
        False,
        5,
        DEFAULT_BACKOFF,
    ),
    (
        "small_budget_two_attempts",
        [(503, {"error": "unavailable"})],
        2,
        False,
        2,
        [1],
    ),
    (
        "network_exceptions",
        ["exc"],
        5,
        False,
        5,
        DEFAULT_BACKOFF,
    ),
    # -- strictness gaps: accepted today, must be rejected ------------------
    (
        "substring_not_okay",
        [(200, {"stdout": "not okay"})],
        5,
        False,
        5,
        DEFAULT_BACKOFF,
    ),
    (
        "substring_ok_but_failed",
        [(200, {"stdout": "ok but execution failed"})],
        5,
        False,
        5,
        DEFAULT_BACKOFF,
    ),
    (
        "stdout_list_containing_ok",
        [(200, {"stdout": ["ok"]})],
        5,
        False,
        5,
        DEFAULT_BACKOFF,
    ),
    (
        "stdout_map_with_ok_key",
        [(200, {"stdout": {"ok": ["1"]}})],
        5,
        False,
        5,
        DEFAULT_BACKOFF,
    ),
    (
        "explicit_error_flag",
        [(200, {"error": "exec crashed", "stdout": "ok"})],
        5,
        False,
        5,
        DEFAULT_BACKOFF,
    ),
    (
        "status_error_flag",
        [(200, {"status": "error", "stdout": "ok"})],
        5,
        False,
        5,
        DEFAULT_BACKOFF,
    ),
    # -- retry schedule bound: must not index past the backoff table --------
    (
        "large_budget_seven_attempts",
        ["exc"],
        7,
        False,
        7,
        [1, 2, 4, 4, 4, 4],
    ),
    (
        "large_budget_twelve_attempts",
        ["exc"],
        12,
        False,
        12,
        [1, 2, 4, 4, 4, 4, 4, 4, 4, 4, 4],
    ),
]


@pytest.mark.parametrize(
    "case,script,max_attempts,expect_ready,expected_posts,expected_sleeps",
    CASES,
    ids=[c[0] for c in CASES],
)
def test_probe_readiness(
    monkeypatch, case, script, max_attempts, expect_ready, expected_posts,
    expected_sleeps,
):
    fake_http, sleeps = _harness(monkeypatch, script)
    if expect_ready:
        assert (
            sandbox_helpers._probe_exec(URL, max_attempts=max_attempts) is None
        )
    else:
        with pytest.raises(TimeoutError):
            sandbox_helpers._probe_exec(URL, max_attempts=max_attempts)

    calls = fake_http.calls
    assert len(calls) == expected_posts, case
    assert sleeps == expected_sleeps, case
    for call in calls:
        assert call["endpoint"] == ENDPOINT
        assert call["json"] == PAYLOAD
        assert call["timeout"] == 10
