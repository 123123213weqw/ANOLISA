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

"""Test batch result persistence (crash checkpointing).

Covers two result-loss bugs in ``run_batch``:

1. End-only persistence: ``batch_results.json`` / ``batch_summary.json``
   were written only after ALL chunks completed, so any uncaught exception
   mid-batch (e.g. ``stop_gateway``'s ``openclaw gateway stop`` raising
   ``TimeoutExpired`` during chunk cleanup) propagated and lost every
   completed chunk's results. Results are now persisted after every chunk
   and chunk cleanup is best-effort.
2. ``make_trace_dir`` minute-resolution collision: two runners started in
   the same minute shared one trace dir and clobbered each other's
   summaries (last writer wins).

``run_batch`` is driven with all external effects faked (gateway, sandbox,
agent, grading); only the chunk-loop bookkeeping and persistence run for
real. No real processes, containers, or the real gateway are touched.
"""

import atexit
import contextlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


# ── Fake batch driver ─────────────────────────────────────────────────


def _drive_run_batch(tmp_path, stop_gateway_effect=None,
                     restart_gateway_effect=None):
    """Run a 2-chunk x 1-task batch with everything faked.

    Returns a dict with the trace dir, the run's outcome
    ("returned" / "raised:<ExcName>" / "exit:<code>"), and helpers to read
    the persisted outputs. The ``ce_runner`` log sink is always detached
    and any atexit hook registered by run_batch is unregistered afterwards.
    """
    import ce_runner.batch_runner as br
    from ce_runner import _common

    work = str(tmp_path)
    trace_dir = os.path.join(work, "trace")
    os.makedirs(trace_dir, exist_ok=True)

    tasks_dir = os.path.join(work, "tasks")
    task_names = []
    for i in (1, 2):
        td = os.path.join(tasks_dir, f"T00{i}_persist")
        os.makedirs(td, exist_ok=True)
        with open(os.path.join(td, "task.yaml"), "w") as f:
            f.write(f"task_id: T00{i}_persist\ntask_name: persist {i}\n"
                    "difficulty: easy\nservices: []\nsandbox_files: []\n")
        task_names.append(f"T00{i}_persist")

    session_file = os.path.join(work, "session.jsonl")
    with open(session_file, "w") as f:
        f.write("{}\n")

    # A unique agent id whose sessions dir cannot exist on this machine:
    # _execute_one wipes stale *.jsonl from it before every trial.
    agent_id = f"claweval-{uuid.uuid4().hex[:8]}"
    assert not os.path.isdir(str(Path.home() / ".openclaw" / "agents"
                                 / agent_id / "sessions"))

    def make_args():
        return SimpleNamespace(
            tasks_dir=tasks_dir, tag=None, range=None, filter=None,
            prefix=None, parallel=1, config=None, timeout=10, trials=1,
            sandbox_image=None, chunk_size=1, tasks_file=None,
            tasks_string=None, trace_prefix="persist", skip_preflight=True,
            grade_parallel=1)

    def fake_slots(task_yamls):
        slots = {}
        for tyaml in task_yamls:
            slots[tyaml] = {
                "task_id": os.path.basename(os.path.dirname(tyaml)),
                "agent_id": agent_id,
                "port_offset": 0,
                "sandbox_url": "http://127.0.0.1:1",
                "sandbox_host_port": 1,
                "sandbox_image": "img",
                "sandbox_runner": MagicMock(),
                "task_def": SimpleNamespace(local_grader_files=[]),
            }
        return {"task_slots": slots}

    def graded_result(task_id):
        return {
            "task_id": task_id, "task_score": 0.5, "passed": False,
            "completion": 0.5, "robustness": 0.5, "communication": 0.5,
            "safety": 0.5, "error": None,
            "trace_file": os.path.join(trace_dir, f"{task_id}.jsonl"),
            "session_archive_file": "", "session_origin_file": "",
        }

    stop_kwargs = ({"side_effect": stop_gateway_effect}
                   if stop_gateway_effect is not None else {})
    restart_kwargs = ({"side_effect": restart_gateway_effect}
                      if restart_gateway_effect is not None
                      else {"return_value": True})

    registered = []
    real_register = atexit.register

    def spying_register(func, *a, **k):
        registered.append(func)
        return real_register(func, *a, **k)

    outcome = None
    try:
        with contextlib.ExitStack() as stack:
            for p in (
                patch.object(br, "require_valid_config"),
                patch.object(br, "check_gateway", return_value=18789),
                patch.object(br, "make_trace_dir", return_value=trace_dir),
                patch.object(br, "ensure_user_session_persistent"),
                patch.object(br, "find_missing_fixtures", return_value={}),
                patch.object(br, "setup_parallel_workers",
                             side_effect=lambda tl, *a, **k: fake_slots(tl)),
                patch.object(br, "start_mock_services_with_offset"),
                patch.object(br, "restart_gateway", **restart_kwargs),
                patch.object(br, "reset_services_with_offset"),
                patch.object(br, "run_agent", return_value=session_file),
                patch.object(br, "collect_env_snapshot", return_value={}),
                patch.object(br, "save_env_snapshot", return_value=""),
                patch.object(br, "fetch_audit_data", return_value={}),
                patch.object(br, "convert_and_grade_sandbox",
                             side_effect=lambda tid, *a, **k: graded_result(tid)),
                patch.object(br, "kill_mcp_bridges"),
                patch.object(br, "stop_gateway", **stop_kwargs),
                patch.object(br, "cleanup_mock_services"),
                patch.object(br, "cleanup_parallel_workers"),
                patch.object(br, "reap_orphan_agent_processes"),
                patch("ce_runner.sandbox_helpers.start_sandbox_container",
                      return_value=MagicMock()),
                patch("ce_runner.sandbox_helpers.stop_sandbox_container"),
                patch("time.sleep"),
                patch.object(br.atexit, "register", side_effect=spying_register),
            ):
                stack.enter_context(p)
            try:
                br.run_batch(
                    make_args(),
                    get_judge_config=lambda cfg: {"api_key": "k", "base_url": "http://j", "model": "j"},
                    get_model_config=lambda cfg: {"api_key": "k", "base_url": "http://m", "model_id": "m"},
                    get_user_agent_config=lambda cfg: {"api_key": "k", "base_url": "http://u", "model_id": "u"},
                    discover_tasks=lambda d, **kw: [os.path.join(tasks_dir, n) for n in task_names],
                )
                outcome = "returned"
            except SystemExit as e:
                outcome = f"exit:{e.code}"
            except Exception as e:
                outcome = f"raised:{type(e).__name__}"
    finally:
        for f in registered:
            atexit.unregister(f)
        _common.detach_log_file()

    def read(name):
        path = os.path.join(trace_dir, name)
        if not os.path.exists(path):
            return None
        with open(path) as fh:
            return fh.read()

    return {
        "trace_dir": trace_dir,
        "outcome": outcome,
        "log": read("batch.log") or "",
        "results": read("batch_results.json"),
        "summary": read("batch_summary.json"),
        "session_map": read("session_map.json"),
    }


def _timeout_stop_gateway(*_args, **_kwargs):
    """The production failure: `openclaw gateway stop` hits its 30s timeout."""
    raise subprocess.TimeoutExpired(["openclaw", "gateway", "stop"], 30)


# ── run_batch persistence tests ───────────────────────────────────────


class TestStopGatewayFailureKeepsResults:
    """A raising cleanup step must not lose completed chunks' results."""

    def test_results_survive_stop_gateway_timeout(self, tmp_path):
        """stop_gateway raising TimeoutExpired at every chunk cleanup:
        results still land on disk and the error is surfaced non-fatally."""
        out = _drive_run_batch(
            tmp_path, stop_gateway_effect=_timeout_stop_gateway)

        # Batch runs to completion instead of dying mid-loop.
        assert out["outcome"] == "returned"

        # The failure is surfaced (logged), not swallowed silently.
        assert "cleanup step stop_gateway failed" in out["log"]

        # Both chunks' trials are persisted.
        results = json.loads(out["results"])
        assert sorted(t["task_id"] for t in results) == \
            ["T001_persist", "T002_persist"]
        for task in results:
            assert task["error"] is None
            assert len(task["trials"]) == 1
            assert task["trials"][0]["task_score"] == 0.5

        summary = json.loads(out["summary"])
        assert summary["tasks"] == 2
        assert summary["errored"] == 0

    def test_normal_path_unaffected(self, tmp_path):
        """With a healthy gateway, outputs are identical in shape and no
        cleanup warnings are logged."""
        out = _drive_run_batch(tmp_path)

        assert out["outcome"] == "returned"
        assert "cleanup step" not in out["log"]
        assert "Results:" in out["log"]
        assert "Session map:" in out["log"]

        results = json.loads(out["results"])
        assert len(results) == 2
        summary = json.loads(out["summary"])
        assert summary["tasks"] == 2
        assert summary["n_chunks"] == 2
        assert summary["trials_per_task"] == 1

        session_map = json.loads(out["session_map"])
        assert len(session_map["entries"]) == 2


class TestPerChunkCheckpoint:
    """Results are persisted after each chunk, not only at the very end."""

    def test_mid_batch_abort_keeps_earlier_chunk(self, tmp_path):
        """Chunk 2's gateway restart failing fatally (RuntimeError -> exit 1)
        must not lose chunk 1's completed trial."""
        # First restart (chunk 1) succeeds, second (chunk 2) raises fatally.
        def restart_sequence(*_a, **_k):
            if restart_sequence.calls == 0:
                restart_sequence.calls += 1
                return True
            raise RuntimeError("gateway user-bus check failed")

        restart_sequence.calls = 0
        out = _drive_run_batch(
            tmp_path, restart_gateway_effect=restart_sequence)

        assert out["outcome"] == "exit:1"
        # Chunk 1's results were checkpointed before the abort.
        assert "[checkpoint]" in out["log"]
        results = json.loads(out["results"])
        assert [t["task_id"] for t in results] == ["T001_persist"]
        assert results[0]["trials"][0]["task_score"] == 0.5
        assert results[0]["error"] is None


# ── make_trace_dir collision tests ────────────────────────────────────


class TestMakeTraceDirUnique:
    """Two runners started in the same minute must not share a trace dir."""

    def test_sequential_calls_get_distinct_dirs(self, tmp_path):
        from ce_runner import _common

        with patch.object(_common, "_REPO_DIR", tmp_path):
            first = _common.make_trace_dir("openclaw")
            second = _common.make_trace_dir("openclaw")

        assert first != second
        assert os.path.isdir(first) and os.path.isdir(second)
        assert second == first + "-2"

    def test_preexisting_dir_gets_suffix(self, tmp_path):
        """A dir already created by another runner (same minute) is left
        alone; a fresh sibling dir is used instead of overwriting it."""
        from ce_runner import _common
        from datetime import datetime

        ts = datetime.now().strftime("%y-%m-%d-%H-%M")
        existing = tmp_path / "claw-eval" / "traces" / f"openclaw_{ts}"
        existing.mkdir(parents=True)
        marker = existing / "batch_results.json"
        marker.write_text("[]")

        with patch.object(_common, "_REPO_DIR", tmp_path):
            got = _common.make_trace_dir("openclaw")

        assert got == str(existing) + "-2"
        assert os.path.isdir(got)
        # The other runner's dir (and file) is untouched.
        assert marker.read_text() == "[]"
