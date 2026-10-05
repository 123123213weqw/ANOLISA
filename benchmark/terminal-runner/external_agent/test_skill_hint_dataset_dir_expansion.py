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

"""``DATASET_DIR`` must be ``~``-expanded like every other path here.

``_load_skill_hint`` joins ``DATASET_DIR`` with the task name and stats
the result directly, so ``DATASET_DIR=~/datasets`` (with the skill file
present at ``~/datasets/<task>/skill.md``) silently produced an empty
hint: the literal ``~`` path never matches a file.  Every other
user-rooted path in this adapter (``~/.openclaw``, ``~/.openclaw-<p>``)
is expanded, and the module docstring documents ``DATASET_DIR`` as a
plain path — so the tilde form is silently-broken config, not a
documented constraint.
"""

from __future__ import annotations

from typing import Any

import pytest
from openclaw_external_agent import OpenClawExternalAgent


def _make_agent(task_name: str = "crack-7z-hash") -> OpenClawExternalAgent:
    agent = OpenClawExternalAgent(model_name="openai/gpt-4o")
    agent._task_name = task_name
    return agent


def test_tilde_dataset_dir_finds_skill_md(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``~``-prefixed DATASET_DIR must resolve against $HOME."""
    home = tmp_path / "home"
    dataset_dir = home / "datasets"
    skill_dir = dataset_dir / "crack-7z-hash"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.md").write_text("Use hashcat in mask mode.")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DATASET_DIR", "~/datasets")

    hint = _make_agent()._load_skill_hint()

    assert hint == "Use hashcat in mask mode.", (
        "tilde DATASET_DIR was not expanded; skill hint silently empty"
    )


def test_explicit_absolute_dataset_dir_still_works(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absolute DATASET_DIR keeps working (no behavior change)."""
    dataset_dir = tmp_path / "datasets"
    skill_dir = dataset_dir / "crack-7z-hash"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.md").write_text("Absolute path hint.")

    monkeypatch.setenv("DATASET_DIR", str(dataset_dir))

    assert _make_agent()._load_skill_hint() == "Absolute path hint."


def test_default_relative_dataset_dir_unchanged(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default relative ``dataset`` dir still resolves against the CWD."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATASET_DIR", raising=False)
    skill_dir = tmp_path / "dataset" / "crack-7z-hash"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.md").write_text("Relative default hint.")

    assert _make_agent()._load_skill_hint() == "Relative default hint."
