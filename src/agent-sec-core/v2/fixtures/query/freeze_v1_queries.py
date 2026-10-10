"""Freeze V1 observability query payloads with the original V1 handlers and SQLite readers.

Run using Python 3.11.6 with agent-sec-cli/src on PYTHONPATH and its dependencies installed.
"""

import json
import os
import tempfile
from pathlib import Path

from agent_sec_cli.daemon.handlers import security_query
from agent_sec_cli.daemon.protocol import DaemonRequest
from agent_sec_cli.observability.schema import validate_observability_record
from agent_sec_cli.observability.sqlite_writer import ObservabilitySqliteWriter
from agent_sec_cli.security_events.schema import SecurityEvent
from agent_sec_cli.security_events.sqlite_writer import SqliteEventWriter


def freeze() -> None:
    """Regenerate one deterministic fixture, without contacting a daemon."""
    defaults = {
        "event_type": "scan",
        "category": "code_scan",
        "result": "succeeded",
        "trace_id": "trace-1",
        "timestamp": "2026-06-09T00:00:01+00:00",
        "pid": 123,
        "uid": 0,
        "session_id": "session-1",
        "run_id": "run-1",
        "tool_call_id": "tool-1",
    }
    events = [
        SecurityEvent(**(defaults | fields)).to_dict()
        for fields in [
            {
                "event_id": "code",
                "details": {
                    "request": {"code": "echo hi"},
                    "result": {"verdict": "pass"},
                },
            },
            {
                "event_id": "batch",
                "category": "skill_ledger",
                "timestamp": "2026-06-09T00:00:02+00:00",
                "details": {
                    "request": {"command": "scan", "skill_dir": "/skills/batch"},
                    "result": {
                        "command": "scan",
                        "results": [{"skill_name": "a"}, {"skill_name": "b"}],
                    },
                },
            },
            {
                "event_id": "unassigned",
                "session_id": None,
                "run_id": None,
                "tool_call_id": None,
                "timestamp": "2026-06-09T00:00:03+00:00",
                "details": {},
            },
        ]
    ]
    observations = [
        {
            "hook": "before_agent_run",
            "observedAt": "2026-06-09T00:00:00+00:00",
            "metadata": {"sessionId": "session-1", "runId": "run-1"},
            "metrics": {"user_input": "inspect coverage"},
        },
        {
            "hook": "before_tool_call",
            "observedAt": "2026-06-09T00:00:01+00:00",
            "metadata": {
                "sessionId": "session-1",
                "runId": "run-1",
                "toolCallId": "tool-1",
            },
            "metrics": {"parameters": {"command": "echo hi"}},
        },
        {
            "hook": "before_agent_run",
            "observedAt": "2026-06-09T00:01:00+00:00",
            "metadata": {"sessionId": "session-2", "runId": "run-2"},
            "metrics": {"user_input": "second session"},
        },
    ]
    methods = {
        "obs.sessions.list": security_query.observability_sessions_list_handler,
        "obs.runs.list": security_query.observability_runs_list_handler,
        "obs.timeline.get": security_query.observability_timeline_get_handler,
    }
    cases = [
        ("obs.sessions.list", {}),
        ("obs.sessions.list", {"limit": 1}),
        ("obs.sessions.list", {"offset": 99}),
        ("obs.runs.list", {"session_id": "session-1"}),
        ("obs.runs.list", {"session_id": "missing"}),
        ("obs.timeline.get", {"session_id": "session-1", "run_id": "run-1"}),
        (
            "obs.timeline.get",
            {"session_id": "session-1", "run_id": "run-1", "include_security": False},
        ),
        (
            "obs.timeline.get",
            {"session_id": "session-1", "run_id": "run-1", "limit": 1, "offset": 1},
        ),
        ("obs.timeline.get", {"session_id": "missing", "run_id": "missing"}),
    ]
    with tempfile.TemporaryDirectory(prefix="v1-query-") as directory:
        os.environ["AGENT_SEC_DATA_DIR"] = directory
        writer = SqliteEventWriter(max_age_days=None)
        for event in events:
            writer.write(SecurityEvent(**event))
        writer.close()
        obs_writer = ObservabilitySqliteWriter(max_age_days=None)
        for observation in observations:
            obs_writer.write_or_raise(validate_observability_record(observation))
        obs_writer.close()
        frozen = [
            {
                "method": method,
                "params": params,
                "expected": methods[method](
                    DaemonRequest(method=method, params=params), None
                ).data,
            }
            for method, params in cases
        ]
    Path(__file__).with_name("v1-query-responses.json").write_text(
        json.dumps(
            {"events": events, "observations": observations, "cases": frozen}, indent=2
        )
        + "\n"
    )


if __name__ == "__main__":
    freeze()
