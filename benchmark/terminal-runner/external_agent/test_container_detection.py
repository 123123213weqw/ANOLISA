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

"""The fallback Harbor container detection must stay session-scoped.

``_detect_harbor_container`` first filters ``docker ps`` by the
environment's ``session_id``.  When that filter misses (e.g. the session
id carries characters or case that Harbor's compose project-name
sanitization rewrote), the fallback scans every running container for
names containing ``"__"`` and ``"-main-"`` — with no session scoping.
Two parallel ``harbor run`` sessions both match that loose pattern, so
the fallback could latch onto the OTHER session's container and execute
the whole benchmark inside it.

All docker invocations below are fakes; no real container is touched.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from openclaw_external_agent import OpenClawExternalAgent

# A parallel session's container: matches the loose "__"/"-main-" pattern.
FOREIGN_LINE = "aaaa1111aaaa crack-7z-hash__aa77-main-1"
# This session's container: harbor lowercased the session id and turned
# "/" into "-" for the compose project name, so the *raw* session id is
# NOT a substring of the container name — exactly when the fallback runs.
OWN_LINE = "bbbb2222bbbb crack-7z-hash__bb77-x-main-1"
OWN_SESSION_ID = "Crack-7Z-hash__BB77/x"


class _FakeProc:
    def __init__(self, stdout: bytes = b"", returncode: int = 0) -> None:
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""


class _FakeDocker:
    """Answer the docker CLI calls made by ``_detect_harbor_container``."""

    def __init__(self, ps_lines: list[str], filter_stdout: bytes = b"") -> None:
        self.ps_lines = ps_lines
        self.filter_stdout = filter_stdout
        self.inspect_ids: list[str] = []

    async def __call__(self, *argv: Any, **_kwargs: Any) -> _FakeProc:
        if "ps" in argv and "-q" in argv:
            return _FakeProc(stdout=self.filter_stdout)
        if "ps" in argv:
            return _FakeProc(stdout=("\n".join(self.ps_lines)).encode())
        if "inspect" in argv:
            self.inspect_ids.append(argv[-1])
            return _FakeProc(returncode=0)
        raise AssertionError(f"unexpected docker call: {argv!r}")


class _Env:
    def __init__(self, session_id: str | None) -> None:
        if session_id is not None:
            self.session_id = session_id


def _agent() -> OpenClawExternalAgent:
    agent = OpenClawExternalAgent(model_name="openai/gpt-4o")
    agent._task_name = "dummy-task"
    agent._profile_name = "deadbeef"
    return agent


@pytest.mark.asyncio
async def test_fallback_does_not_adopt_a_foreign_session_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other session's container matches the loose pattern — skip it."""
    fake = _FakeDocker(ps_lines=[FOREIGN_LINE, OWN_LINE])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    detected = await _agent()._detect_harbor_container(_Env(OWN_SESSION_ID))

    assert detected == "bbbb2222bbbb", (
        f"adopted foreign container {detected!r}; expected own 'bbbb2222bbbb'"
    )


@pytest.mark.asyncio
async def test_fallback_returns_none_when_only_foreign_containers_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No own container found is better than stealing someone else's."""
    fake = _FakeDocker(ps_lines=[FOREIGN_LINE])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    detected = await _agent()._detect_harbor_container(_Env(OWN_SESSION_ID))

    assert detected is None


@pytest.mark.asyncio
async def test_fallback_without_session_id_keeps_legacy_heuristic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Environments without a session id keep the best-effort fallback."""
    fake = _FakeDocker(ps_lines=[FOREIGN_LINE])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    detected = await _agent()._detect_harbor_container(_Env(None))

    assert detected == "aaaa1111aaaa"


@pytest.mark.asyncio
async def test_session_filter_still_takes_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session-id filter hit returns immediately, without the fallback."""
    fake = _FakeDocker(ps_lines=[], filter_stdout=b"cccc3333cccc\n")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    detected = await _agent()._detect_harbor_container(_Env("plain-session"))

    assert detected == "cccc3333cccc"
    assert fake.inspect_ids == []
