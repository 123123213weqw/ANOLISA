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

"""trace_end efficiency values survive the grader score merge.

Graders receive ``(messages, dispatches, task)`` — never ``trace_end`` — so
they cannot compute wall-time/token efficiency and return the 0.0 defaults.
``grade_trace`` must keep the converter-measured ``efficiency_*`` values
already written into ``trace_end`` instead of overwriting them with those
defaults, while still honouring grader-computed (non-zero) values.

``claw_eval`` is a sibling checkout that is not vendored in this repo, so the
test installs stub modules matching the documented import surface before
importing ``ce_runner.grader_runner``.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

TASK_ID = "M001_clock"

# Grader-side scores, switchable per test via STUB["scores"].
STUB = {
    "scores": SimpleNamespace(
        completion=0.4, robustness=0.8, communication=0.9, safety=1.0,
        efficiency_turns=0.0, efficiency_tokens=0.0, efficiency_wall_time_s=0.0,
    ),
    "task_score": 0.42,
    "passed": False,
}


class _StubGrader:
    def grade(self, messages, dispatches, task, audit_data=None, judge=None):
        return STUB["scores"]


def _install_claw_eval_stubs(monkeypatch) -> None:
    """Install claw_eval stub modules and (re)import ce_runner.grader_runner."""
    claw_eval = types.ModuleType("claw_eval")
    graders = types.ModuleType("claw_eval.graders")
    registry = types.ModuleType("claw_eval.graders.registry")
    registry.get_grader = lambda task_id, **kw: _StubGrader()
    llm_judge = types.ModuleType("claw_eval.graders.llm_judge")

    class _LLMJudge:  # noqa: N801 - mirror real class name
        def __init__(self, model_id="", api_key=None, base_url=""):
            pass

    llm_judge.LLMJudge = _LLMJudge

    models = types.ModuleType("claw_eval.models")
    scoring = types.ModuleType("claw_eval.models.scoring")
    scoring.compute_task_score = lambda scores: STUB["task_score"]
    scoring.is_pass = lambda task_score: STUB["passed"]
    task_mod = types.ModuleType("claw_eval.models.task")

    class _TaskDefinition:  # noqa: N801 - mirror real class name
        @staticmethod
        def from_yaml(path):
            return SimpleNamespace(task_id=TASK_ID)

    task_mod.TaskDefinition = _TaskDefinition

    trace = types.ModuleType("claw_eval.trace")
    reader = types.ModuleType("claw_eval.trace.reader")

    def _load_trace(path):
        start, messages, dispatches, end, audit = None, [], [], None, {}
        with open(path) as f:
            for line in f:
                ev = json.loads(line)
                if ev.get("type") == "trace_start":
                    start = SimpleNamespace(trace_id=ev["trace_id"],
                                            task_id=ev["task_id"])
                elif ev.get("type") == "audit_snapshot":
                    audit[ev.get("service_name", "")] = ev.get("audit_data")
        assert start is not None, f"no trace_start in {path}"
        return start, messages, dispatches, [], end, audit

    reader.load_trace = _load_trace

    for name, module in {
        "claw_eval": claw_eval,
        "claw_eval.graders": graders,
        "claw_eval.graders.registry": registry,
        "claw_eval.graders.llm_judge": llm_judge,
        "claw_eval.models": models,
        "claw_eval.models.scoring": scoring,
        "claw_eval.models.task": task_mod,
        "claw_eval.trace": trace,
        "claw_eval.trace.reader": reader,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    import ce_runner.grader_runner  # noqa: F401 - binds stubs above
    return sys.modules["ce_runner.grader_runner"]


def _write_fixtures(tmp_path: Path, efficiency: dict | None) -> tuple:
    """Write a task.yaml plus a trace whose trace_end carries converter values."""
    task_dir = tmp_path / "tasks" / TASK_ID
    task_dir.mkdir(parents=True)
    task_yaml = task_dir / "task.yaml"
    task_yaml.write_text(f"task_id: {TASK_ID}\ntask_name: Clock\n")

    scores = {
        "completion": 0.0, "robustness": 0.0,
        "communication": 0.0, "safety": 1.0,
        "efficiency_turns": 12,
        "efficiency_tokens": 34567,
        "efficiency_wall_time_s": 431.2,
    }
    if efficiency is not None:
        scores.update(efficiency)

    trace_path = tmp_path / f"{TASK_ID}_8a61ea06.jsonl"
    events = [
        {"type": "trace_start", "trace_id": "trace-1", "task_id": TASK_ID,
         "timestamp": "2026-01-01T00:00:00+00:00"},
        {"type": "trace_end", "trace_id": "trace-1",
         "total_turns": 12, "input_tokens": 34567, "output_tokens": 1234,
         "wall_time_s": 431.2, "scores": scores,
         "task_score": 0.0, "passed": False},
    ]
    with open(trace_path, "w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    return str(task_yaml), str(trace_path)


def _trace_end(trace_path: str) -> dict:
    with open(trace_path) as f:
        for line in f:
            ev = json.loads(line)
            if ev.get("type") == "trace_end":
                return ev
    raise AssertionError("trace_end missing after grading")


def test_grader_defaults_preserve_converter_efficiency(tmp_path, monkeypatch):
    """Default (0.0) grader efficiency must not clobber trace_end values."""
    grader_runner = _install_claw_eval_stubs(monkeypatch)
    STUB["scores"] = SimpleNamespace(
        completion=0.4, robustness=0.8, communication=0.9, safety=1.0,
        efficiency_turns=0.0, efficiency_tokens=0.0, efficiency_wall_time_s=0.0,
    )
    task_yaml, trace_path = _write_fixtures(tmp_path, efficiency=None)

    grader_runner.grade_trace(trace_path, task_yaml, judge_config=None)

    end = _trace_end(trace_path)
    assert end["scores"]["efficiency_turns"] == 12
    assert end["scores"]["efficiency_tokens"] == 34567
    assert end["scores"]["efficiency_wall_time_s"] == pytest.approx(431.2)
    # Non-efficiency dimensions still come from the grader.
    assert end["scores"]["completion"] == pytest.approx(0.4)
    assert end["scores"]["robustness"] == pytest.approx(0.8)


def test_grader_computed_efficiency_replaces_converter_values(tmp_path, monkeypatch):
    """A grader that really computes efficiency wins over the converter."""
    grader_runner = _install_claw_eval_stubs(monkeypatch)
    STUB["scores"] = SimpleNamespace(
        completion=0.9, robustness=0.9, communication=0.9, safety=1.0,
        efficiency_turns=7.0, efficiency_tokens=65000.0,
        efficiency_wall_time_s=120.0,
    )
    task_yaml, trace_path = _write_fixtures(tmp_path, efficiency=None)

    grader_runner.grade_trace(trace_path, task_yaml, judge_config=None)

    end = _trace_end(trace_path)
    assert end["scores"]["efficiency_turns"] == 7.0
    assert end["scores"]["efficiency_tokens"] == 65000.0
    assert end["scores"]["efficiency_wall_time_s"] == 120.0


def test_missing_converter_efficiency_keeps_grader_defaults(tmp_path, monkeypatch):
    """Old traces without efficiency keys degrade to the grader's values."""
    grader_runner = _install_claw_eval_stubs(monkeypatch)
    STUB["scores"] = SimpleNamespace(
        completion=0.4, robustness=0.8, communication=0.9, safety=1.0,
        efficiency_turns=0.0, efficiency_tokens=0.0, efficiency_wall_time_s=0.0,
    )
    task_yaml, trace_path = _write_fixtures(
        tmp_path, efficiency={"efficiency_turns": None,
                              "efficiency_tokens": None,
                              "efficiency_wall_time_s": None})
    with open(trace_path) as f:
        events = [json.loads(line) for line in f]
    for ev in events:
        if ev.get("type") == "trace_end":
            ev["scores"] = {k: v for k, v in ev["scores"].items()
                            if not k.startswith("efficiency_")}
    with open(trace_path, "w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")

    grader_runner.grade_trace(trace_path, task_yaml, judge_config=None)

    end = _trace_end(trace_path)
    assert end["scores"]["efficiency_turns"] == 0.0
    assert end["scores"]["efficiency_tokens"] == 0.0
    assert end["scores"]["efficiency_wall_time_s"] == 0.0
