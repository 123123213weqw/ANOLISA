"""Freeze V1 schema and report outputs using Python 3.11.6 and the V1 environment."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_sec_cli.observability.schema import observability_record_json_schema
from agent_sec_cli.observability.session_report import (
    build_session_report,
    format_text,
)

ROOT = Path(__file__).resolve().parents[2]
schema = observability_record_json_schema()
(ROOT / "fixtures/query/v1-record-schema.json").write_text(
    json.dumps(schema, indent=2, ensure_ascii=False) + "\n"
)


class Reader:
    def __init__(self, case: dict[str, Any]) -> None:
        self.case = case

    def list_sessions(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(**self.case["session"])]

    def list_runs(self, session_id: str) -> list[SimpleNamespace]:
        return [SimpleNamespace(run_id="run")]

    def list_events(self, session_id: str, run_id: str) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(hook=row["hook"], metrics_json=json.dumps(row["metrics"]))
            for row in self.case["events"]
        ]

    def query_correlation_candidates(self, **filters: Any) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(event=SimpleNamespace(**row))
            for row in self.case["security"]
        ]


path = ROOT / "fixtures/query/v1-reports.json"
cases = json.loads(path.read_text())
for case in cases:
    reader = Reader(case)
    report = build_session_report(case["session"]["session_id"], reader, reader)
    case["json"] = report.to_dict()
    case["text"] = format_text(report) + "\n"
path.write_text(json.dumps(cases, indent=2) + "\n")
