"""Freeze V1 single/batch correlation results without importing optional CLI dependencies."""

import ast
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

SOURCE = (
    Path(__file__).resolve().parents[3]
    / "agent-sec-cli/src/agent_sec_cli/observability/correlation.py"
)
tree = ast.parse(SOURCE.read_text())
# The envelope is only a type dependency. Execute every V1 matching function unchanged.
tree.body = [
    node
    for node in tree.body
    if not (
        isinstance(node, ast.ImportFrom)
        and node.module == "agent_sec_cli.security_events.schema"
    )
]
module = types.ModuleType("frozen_v1_correlation")
module.SecurityEvent = SimpleNamespace
sys.modules[module.__name__] = module
exec(compile(tree, str(SOURCE), "exec"), module.__dict__)


class Reader:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events

    def query_correlation_candidates(self, **filters: Any) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                event=SimpleNamespace(**row["event"]),
                timestamp_epoch=row["timestamp_epoch"],
            )
            for row in self.events
            if row["event"]["session_id"] == filters["session_id"]
            and row["event"]["category"] in filters["categories"]
            and (
                filters.get("run_id") is None
                or row["event"]["run_id"] == filters["run_id"]
            )
            and (
                filters.get("tool_call_id") is None
                or row["event"]["tool_call_id"] == filters["tool_call_id"]
            )
            and (
                filters.get("tool_call_ids") is None
                or row["event"]["tool_call_id"] in filters["tool_call_ids"]
            )
            and (
                filters.get("since_epoch") is None
                or row["timestamp_epoch"] >= filters["since_epoch"]
            )
            and (
                filters.get("until_epoch") is None
                or row["timestamp_epoch"] <= filters["until_epoch"]
            )
        ]


path = Path(__file__).with_name("v1-correlation.json")
fixture = json.loads(path.read_text())
for case in fixture["cases"]:
    record = fixture["record_defaults"] | case["record"]
    candidates = [
        row | {"event": fixture["event_defaults"] | row["event"]}
        for row in case["candidates"]
    ]
    fields = module.ObservabilityRecordFields(
        record["hook"],
        record["session_id"],
        record["run_id"],
        record["tool_call_id"],
        record["timestamp_epoch"],
        record["metrics"],
    )
    service = module.SecurityCorrelationService(Reader(candidates))
    single = service.find_correlated(fields)
    assert single == service.find_correlated_many([fields, fields])[0]
    case["expected"] = [
        {
            "event_id": match.event.event_id,
            "reason": match.match_reason,
            "rank": match.match_rank,
            "time_delta_seconds": match.time_delta_seconds,
        }
        for match in single
    ]
path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n")
