"""Robustness regressions for validate_results error paths.

Covers two error classes reported against the benchmark validator:

- read-only SQLite readers must preserve capture database paths whose
  directory or file names contain literal ``#``, ``?`` or percent-encoded
  looking segments instead of letting them be reinterpreted as URI syntax;
- ``load_expected`` must skip malformed point shapes (null/list/scalar
  ``data`` payloads, list ``tags``) and keep reading later valid request
  evidence instead of raising.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

SINGLE_RUN_DIR = Path(__file__).parents[1] / "single_run"
sys.path.insert(0, str(SINGLE_RUN_DIR))

import validate_results


def make_capture_db(db_path: Path) -> None:
    """Create a real SQLite fixture with one token record per request ID."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE token_records (
                request_id TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER
            );
            CREATE TABLE genai_events (
                call_id TEXT,
                trace_id TEXT,
                status TEXT,
                total_tokens INTEGER,
                event_json TEXT
            );
            INSERT INTO token_records VALUES ('bench-1', 12, 8);
            INSERT INTO genai_events VALUES (NULL, NULL, 'complete', 20,
                                             '{"request_id":"bench-2"}');
            """
        )


@pytest.mark.parametrize(
    "directory_name",
    ["plain", "run#1", "run?old", "100%20path", "space dir"],
)
def test_readers_open_databases_under_special_path_segments(
    tmp_path: Path, directory_name: str
) -> None:
    db_path = tmp_path / directory_name / "agentsight.db"
    make_capture_db(db_path)

    captured = validate_results.load_captured(db_path, "bench-")
    assert captured == {
        "bench-1": [("complete", 20)],
        "bench-2": [("complete", 20)],
    }

    incremental, _, _, _ = validate_results.load_captured_incremental(
        db_path, "bench-", None, 0, 0, set()
    )
    assert incremental == {
        "bench-1": [("complete", 20)],
        "bench-2": [("complete", 20)],
    }


def test_special_path_readers_do_not_create_stray_databases(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "run#1?old" / "agentsight.db"
    make_capture_db(db_path)

    assert validate_results.load_captured(db_path, "bench-") != {}
    parent = db_path.parent
    assert sorted(p.name for p in parent.iterdir()) == ["agentsight.db"], (
        "a misparsed URI silently created or opened another database"
    )
    assert not (tmp_path / "run").exists()


def test_read_only_uri_is_absolute_and_percent_escaped() -> None:
    uri = validate_results.read_only_uri(Path("rel#dir?x/agents%20.db"))
    assert uri.startswith("file:///")
    assert uri.endswith("?mode=ro")
    # The raw characters never appear unescaped in the URI body; SQLite's
    # URI parser decodes the percent escapes back to the literal path.
    body = uri[: -len("?mode=ro")]
    assert "#" not in body and "?" not in body
    assert "%23" in body and "%3F" in body and "%2520" in body


MALFORMED_LINES = [
    {"metric": "benchmark_requests", "data": None, "request_id": "req-null-data"},
    {"metric": "benchmark_requests", "data": ["list"], "request_id": "req-list-data"},
    {"metric": "benchmark_requests", "data": 42, "request_id": "req-scalar-data"},
    {"metric": "benchmark_requests", "data": "text", "request_id": "req-text-data"},
    {
        "metric": "benchmark_requests",
        "data": {"tags": ["not", "a", "dict"]},
        "request_id": "req-list-tags",
    },
]


def write_load_output(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )


@pytest.mark.parametrize("malformed", MALFORMED_LINES)
def test_load_expected_skips_single_malformed_point_shapes(
    tmp_path: Path, malformed: dict
) -> None:
    load_path = tmp_path / "k6.jsonl"
    good = {
        "metric": "benchmark_requests",
        "data": {"value": 1, "tags": {"request_id": "req-good"}},
    }
    write_load_output(load_path, [malformed, good])

    expected, successful = validate_results.load_expected(load_path)
    assert expected == {"req-good"}
    assert successful == set()


def test_load_expected_keeps_evidence_after_malformed_points(
    tmp_path: Path,
) -> None:
    load_path = tmp_path / "k6.jsonl"
    tagged_success = {
        "metric": "benchmark_http_success",
        "data": {"value": 1, "tags": {"request_id": "req-after"}},
    }
    status_success = {
        "metric": "benchmark_http_status",
        "data": {"value": 204, "status": 204, "tags": {"request_id": "req-status"}},
    }
    top_level = {"metric": "benchmark_requests", "request_id": "req-top"}
    write_load_output(
        load_path, [*MALFORMED_LINES, tagged_success, status_success, top_level]
    )

    expected, successful = validate_results.load_expected(load_path)
    assert expected == {"req-after", "req-status", "req-top"}
    assert successful == {"req-after", "req-status"}
