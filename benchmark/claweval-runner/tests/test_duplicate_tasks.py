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

"""Duplicate tasks must be rejected before batch execution.

``task_slots`` is keyed by task.yaml, so a duplicated task collapses both
entries onto one slot (one agent_id, one sandbox host port) while
``chunk_tasks`` still executes it twice — two concurrent trials then share
an agent and fight over the same port. Mirrors swe-runner's duplicate
instance-id rejection: fail fast with the duplicate names.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

JUDGE_CONFIG = {"api_key": "jk", "base_url": "http://judge", "model": "judge-m"}
MODEL_CONFIG = {"api_key": "mk", "base_url": "http://model", "model_id": "model-m"}


def _make_args(tasks_dir: Path, **overrides) -> SimpleNamespace:
    args = SimpleNamespace(
        tasks_dir=str(tasks_dir),
        tag=None,
        range=None,
        filter=None,
        prefix=None,
        parallel=2,
        config=None,
        timeout=60,
        trials=1,
        sandbox_image=None,
        chunk_size=None,
        tasks_file=None,
        tasks_string=None,
        trace_prefix=None,
        skip_preflight=True,
        grade_parallel=0,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _make_tasks_dir(tmp_path: Path, *names: str) -> Path:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    for name in names:
        task_dir = tasks_dir / name
        task_dir.mkdir()
        (task_dir / "task.yaml").write_text(
            f"task_id: {name}\ntask_name: {name}\ndifficulty: medium\n")
    return tasks_dir


def _run_batch(args, monkeypatch, tmp_path):
    from ce_runner import batch_runner

    monkeypatch.setenv("JUDGE_API_KEY", "jk")
    monkeypatch.setenv("MODEL_API_KEY", "mk")
    monkeypatch.setattr(batch_runner, "check_gateway", lambda cfg: None)
    setup = patch.object(batch_runner, "setup_parallel_workers")
    with setup as setup_mock:
        batch_runner.run_batch(
            args,
            lambda cfg: dict(JUDGE_CONFIG),
            lambda cfg: dict(MODEL_CONFIG),
            lambda cfg: {},
            lambda td, **kw: [],
        )
    setup_mock.assert_not_called()
    return setup_mock


def test_duplicate_tasks_string_is_rejected(tmp_path, monkeypatch, capsys):
    tasks_dir = _make_tasks_dir(tmp_path, "M001_clock", "M002_timer")
    args = _make_args(tasks_dir, tasks_string="M001_clock,M002_timer,M001_clock")

    with pytest.raises(SystemExit) as excinfo:
        _run_batch(args, monkeypatch, tmp_path)

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "Duplicate tasks" in out
    assert "M001_clock" in out


def test_duplicate_tasks_file_is_rejected(tmp_path, monkeypatch, capsys):
    tasks_dir = _make_tasks_dir(tmp_path, "M001_clock")
    tasks_file = tmp_path / "tasks.txt"
    tasks_file.write_text("M001_clock\nM001_clock\nM001_clock\n")
    args = _make_args(tasks_dir, tasks_file=str(tasks_file))

    with pytest.raises(SystemExit) as excinfo:
        _run_batch(args, monkeypatch, tmp_path)

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "Duplicate tasks" in out
    assert "M001_clock" in out


def test_unique_tasks_are_not_rejected(tmp_path, monkeypatch, capsys):
    """Unique input passes task selection (stops later at the gateway check)."""
    tasks_dir = _make_tasks_dir(tmp_path, "M001_clock", "M002_timer")
    args = _make_args(tasks_dir, tasks_string="M001_clock,M002_timer")

    with pytest.raises(SystemExit) as excinfo:
        _run_batch(args, monkeypatch, tmp_path)

    out = capsys.readouterr().out
    assert "Duplicate tasks" not in out
    # Reached the gateway check that follows task selection.
    assert "gateway" in out.lower()
