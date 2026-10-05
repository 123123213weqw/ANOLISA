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

"""Per-trial evidence read failures must not abort the CE report producers.

Covers both producers (``scripts/generate_trial_reports.py`` main loop and
``scripts/analyze.py`` ``generate_reports``) over real local task/trace
fixtures with a deterministic judge stub. No model, gateway, Docker or host
service is invoked.
"""

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import analyze  # noqa: E402
import generate_trial_reports as gtr  # noqa: E402

GOOD_TASK_YAML = """\
task_id: {task_id}
task_name: {task_id} name
category: smoke
difficulty: easy
prompt:
  text: do the thing
scoring_components: []
judge_rubric: rubric
primary_dimensions: []
"""

BAD_YAML_TEXT = "task_id: [unclosed\n  - :\n"
BAD_JSON_LINE = '{"type": "grading_result", "passed": true,,}\n'
BAD_UTF8_BYTES = b"\xff\xfe\x00 not utf8\n"


def _good_trace_jsonl() -> str:
    lines = [
        json.dumps(
            {
                "type": "grading_result",
                "passed": True,
                "task_score": 1.0,
                "scores": {"completion": 1.0},
                "judge_calls": [],
            }
        ),
        json.dumps({"type": "trace_end", "wall_time_s": 2, "total_turns": 2}),
    ]
    return "\n".join(lines) + "\n"


def _corrupt_payload(kind: str, source: str):
    """Return (write_mode, content) for the requested failure kind."""
    if kind == "parse":
        return ("text", BAD_YAML_TEXT if source == "task" else BAD_JSON_LINE)
    if kind == "unicode":
        return ("bytes", BAD_UTF8_BYTES)
    # OS read failure: valid content, unreadable permissions
    return (
        "text",
        (
            GOOD_TASK_YAML.format(task_id="mz_bad")
            if source == "task"
            else _good_trace_jsonl()
        ),
    )


class _Env:
    """Fixtures: valid aa/zz neighbours around one corrupt mz trial."""

    def __init__(self, tmp_path: Path, source: str, kind: str):
        self.tasks = tmp_path / "tasks"
        self.traces = tmp_path / "traces"
        self.reports = tmp_path / "reports"
        for d in (self.tasks, self.traces):
            d.mkdir()
        self.corrupt_trace = self.traces / "mz_bad_deadbeef.jsonl"

        for task_id in ("aa_good", "mz_bad", "zz_good"):
            tdir = self.tasks / task_id
            tdir.mkdir()
            (tdir / "task.yaml").write_text(
                GOOD_TASK_YAML.format(task_id=task_id), encoding="utf-8"
            )
            (
                self.traces
                / f"{task_id}_{'a1b2' if task_id != 'mz_bad' else 'deadbeef'}.jsonl"
            ).write_text(_good_trace_jsonl(), encoding="utf-8")

        mode, content = _corrupt_payload(kind, source)
        target = (
            ((self.tasks / "mz_bad") / "task.yaml")
            if source == "task"
            else self.corrupt_trace
        )
        if mode == "bytes":
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
        if kind == "os":
            target.chmod(0o000)
        self.target = target

        # Stale prior success for the corrupt trial, to prove replacement.
        self.reports.mkdir()
        (self.reports / "mz_bad_deadbeef.json").write_text(
            json.dumps(
                {
                    "trace_file": "mz_bad_deadbeef.jsonl",
                    "status": "succ",
                    "task_score": 1.0,
                }
            ),
            encoding="utf-8",
        )

    def restore_perm(self):
        if not os.access(self.target, os.R_OK) and self.target.exists():
            self.target.chmod(0o644)

    def load_report(self, trace_name: str) -> dict:
        with open(
            self.reports / trace_name.replace(".jsonl", ".json"), encoding="utf-8"
        ) as f:
            return json.load(f)


def _expected_exception_name(kind: str, source: str) -> str:
    if kind == "parse":
        if source == "task":
            import yaml

            with pytest.raises(yaml.YAMLError) as ei:
                yaml.safe_load(BAD_YAML_TEXT)
            return type(ei.value).__name__
        return "JSONDecodeError"
    if kind == "unicode":
        return "UnicodeDecodeError"
    return "PermissionError"


PRODUCERS = ["gtr", "analyze"]
SOURCES = ["task", "trace"]
KINDS = ["parse", "unicode", "os"]


def _install_judge_stub(monkeypatch, module):
    calls = []

    def stub(*args, **kwargs):
        calls.append(args)
        return {"category": "other", "key_reason_zh": "stub"}

    monkeypatch.setattr(module, "llm_classify_failure", stub)
    return calls


def _run_producer(producer: str, env: _Env, monkeypatch):
    if producer == "gtr":
        calls = _install_judge_stub(monkeypatch, gtr)
        argv = [
            "generate_trial_reports.py",
            "--trace-dir",
            str(env.traces),
            "--tasks-dir",
            str(env.tasks),
            "--output-dir",
            str(env.reports),
            "--judge-api-key",
            "sk-stub",
            "--judge-model",
            "stub-model",
        ]
        monkeypatch.setattr(sys, "argv", argv)
        gtr.main()
    else:
        calls = _install_judge_stub(monkeypatch, analyze)
        analyze.generate_reports(
            str(env.traces),
            str(env.tasks),
            str(env.reports),
            "sk-stub",
            "https://stub.invalid/v1",
            "stub-model",
        )
    return calls


@pytest.mark.parametrize("producer", PRODUCERS)
@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("kind", KINDS)
def test_unreadable_evidence_is_isolated_per_trial(
    producer, source, kind, tmp_path, monkeypatch, capsys
):
    env = _Env(tmp_path, source, kind)
    try:
        calls = _run_producer(producer, env, monkeypatch)
    finally:
        env.restore_perm()

    # Valid neighbours on both sides still produce success reports.
    assert env.load_report("aa_good_a1b2.jsonl")["status"] == "succ"
    assert env.load_report("zz_good_a1b2.jsonl")["status"] == "succ"

    # The corrupt trial's stale success report is replaced by an explicit
    # error report naming the failed source and the exception.
    report = env.load_report("mz_bad_deadbeef.jsonl")
    assert report["status"] == "error"
    err = report["evidence_read_error"]
    assert err["source"] == str(env.target)
    assert err["exception"] == _expected_exception_name(kind, source)
    assert err["message"]

    # Unreadable evidence is never classified by the judge.
    assert calls == []


@pytest.mark.parametrize("producer", PRODUCERS)
def test_unexpected_loader_errors_are_not_hidden(producer, tmp_path, monkeypatch):
    env = _Env(tmp_path, source="task", kind="parse")
    # Empty task.yaml parses to None; the loader's .get() then raises
    # AttributeError -- a programming error that must keep propagating.
    (env.tasks / "mz_bad" / "task.yaml").write_text("", encoding="utf-8")
    if producer == "gtr":
        _install_judge_stub(monkeypatch, gtr)
        argv = [
            "generate_trial_reports.py",
            "--trace-dir",
            str(env.traces),
            "--tasks-dir",
            str(env.tasks),
            "--output-dir",
            str(env.reports),
            "--judge-api-key",
            "sk-stub",
            "--judge-model",
            "stub-model",
        ]
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(AttributeError):
            gtr.main()
    else:
        _install_judge_stub(monkeypatch, analyze)
        with pytest.raises(AttributeError):
            analyze.generate_reports(
                str(env.traces),
                str(env.tasks),
                str(env.reports),
                "sk-stub",
                "https://stub.invalid/v1",
                "stub-model",
            )
