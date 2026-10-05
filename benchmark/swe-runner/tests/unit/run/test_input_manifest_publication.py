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

"""Publication-safety tests for write_input_manifest.

Regression tests for the truncating writer (issue #5948): the manifest
destination was opened for writing before the payload was validated as
UTF-8, so an unpaired surrogate aborted the write and destroyed the
previous provenance file, and interrupted writes could expose partial
JSON. The writer must serialize and encode before touching the target,
publish through an exclusively owned temporary file on the same
filesystem, keep the previous target on pre-publication errors, and
clean only its own temporary.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from swe_runner.agents.lifecycle import PreparedAgentRun
from swe_runner.common.models import AgentConfig, Settings, SWEInstance
from swe_runner.run.io.input_manifest import write_input_manifest

_RUNNER_GIT_INFO = {
    "repo_root": "/repo",
    "commit": "rev",
    "dirty": False,
    "status_porcelain_sha256": "",
}


def _make_instance(instance_id: str = "django__django-1") -> SWEInstance:
    return SWEInstance(
        instance_id=instance_id,
        repo="django/django",
        version="3.2",
        base_commit="abc123",
        problem_statement="Fix the bug",
        patch="gold patch",
        test_patch="test patch",
    )


def _make_prepared(tmp_path: Path, metadata: dict) -> PreparedAgentRun:
    return PreparedAgentRun(
        instance=_make_instance(),
        settings=Settings(
            agent=AgentConfig(name="openclaw", timeout=1200, step_limit=0, workers=2),
            output={"output_dir": tmp_path / "run"},
        ),
        work_dir=tmp_path / "repo",
        prompt="Solve the following issue.",
        timeout=1200,
        max_turns=0,
        base_revision="abc123",
        metadata={
            "docker_image_name": "swebench/sweb.eval.x86_64.django_1776_django-1:latest",
            **metadata,
        },
    )


def _write(tmp_path: Path, prepared: PreparedAgentRun) -> Path:
    with patch(
        "swe_runner.run.io.input_manifest._runner_git_info",
        return_value=_RUNNER_GIT_INFO,
    ):
        return write_input_manifest(tmp_path / "run", agent_name="openclaw", prepared=prepared)


def _manifest_path(tmp_path: Path) -> Path:
    return tmp_path / "run" / "input-manifests" / "django__django-1" / "input_manifest.json"


def test_encoding_failure_preserves_previous_manifest(tmp_path: Path) -> None:
    first = _write(tmp_path, _make_prepared(tmp_path, {}))
    previous = first.read_bytes()

    # An unpaired surrogate only surfaces at UTF-8 encoding time.
    surrogate = _make_prepared(tmp_path, {"custom_note": "bad \ud800 surrogate"})
    with pytest.raises(UnicodeEncodeError):
        _write(tmp_path, surrogate)

    assert first.read_bytes() == previous
    assert json.loads(first.read_text(encoding="utf-8"))["instance_id"] == "django__django-1"


def test_publication_failure_preserves_previous_manifest(tmp_path: Path) -> None:
    first = _write(tmp_path, _make_prepared(tmp_path, {}))
    previous = first.read_bytes()

    replacement = _make_prepared(tmp_path, {"custom_note": "second run"})
    with patch("os.replace", side_effect=OSError("publication failed")):
        with pytest.raises(OSError, match="publication failed"):
            _write(tmp_path, replacement)

    assert first.read_bytes() == previous
    leftovers = [p for p in first.parent.iterdir() if p != first]
    assert leftovers == []


def test_manifest_serialization_format_and_destination_unchanged(tmp_path: Path) -> None:
    manifest_path = _write(tmp_path, _make_prepared(tmp_path, {"custom_note": "format"}))

    text = manifest_path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    payload = json.loads(text)
    assert text == json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    assert payload["manifest_path"] == str(manifest_path)
    assert manifest_path == _manifest_path(tmp_path)


def test_republication_replaces_prior_evidence_completely(tmp_path: Path) -> None:
    first = _write(tmp_path, _make_prepared(tmp_path, {"custom_note": "first"}))
    assert json.loads(first.read_text(encoding="utf-8"))["metadata"]["custom_note"] == "first"

    second = _write(tmp_path, _make_prepared(tmp_path, {"custom_note": "second"}))
    assert second == first
    data = json.loads(second.read_text(encoding="utf-8"))
    assert data["metadata"]["custom_note"] == "second"
    assert [p.name for p in second.parent.iterdir()] == ["input_manifest.json"]
