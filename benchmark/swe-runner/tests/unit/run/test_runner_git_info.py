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

"""Regressions for runner Git provenance recorded in input manifests."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from swe_runner.run.io.input_manifest import _runner_git_info

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "author@example.test",
    "GIT_COMMITTER_NAME": "Test Author",
    "GIT_COMMITTER_EMAIL": "author@example.test",
}


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**_GIT_ENV, "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    return result.stdout.strip()


def _make_source_tree(root: Path) -> Path:
    source_dir = root / "src" / "swe_runner" / "run" / "io"
    source_dir.mkdir(parents=True)
    (source_dir / "input_manifest.py").write_text("# runner source\n", encoding="utf-8")
    return source_dir


def _make_git_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo_root = tmp_path / "anolisa-checkout"
    repo_root.mkdir()
    source_dir = _make_source_tree(repo_root)
    _git(repo_root, "init", "-q")
    _git(repo_root, "config", "user.email", "author@example.test")
    _git(repo_root, "config", "user.name", "Test Author")
    _git(repo_root, "add", "-A")
    _git(repo_root, "commit", "-q", "-m", "initial")
    return repo_root, source_dir


def test_runner_git_info_resolves_actual_worktree_root(tmp_path: Path) -> None:
    repo_root, source_dir = _make_git_repo(tmp_path)
    info = _runner_git_info(source_dir)
    assert info["repo_root"] == str(repo_root.resolve())
    assert info["commit"] == _git(repo_root, "rev-parse", "HEAD")
    assert info["dirty"] is False
    assert info["status_porcelain_sha256"] == hashlib.sha256(b"").hexdigest()


def test_runner_git_info_resamples_after_untracked_edit(tmp_path: Path) -> None:
    repo_root, source_dir = _make_git_repo(tmp_path)
    first = _runner_git_info(source_dir)
    (repo_root / "untracked.txt").write_text("later edit\n", encoding="utf-8")
    second = _runner_git_info(source_dir)
    assert second["commit"] == first["commit"]
    assert second["dirty"] is True
    assert second["status_porcelain_sha256"] != first["status_porcelain_sha256"]


def test_runner_git_info_resamples_after_later_commit(tmp_path: Path) -> None:
    repo_root, source_dir = _make_git_repo(tmp_path)
    first = _runner_git_info(source_dir)
    (repo_root / "second.txt").write_text("second change\n", encoding="utf-8")
    _git(repo_root, "add", "-A")
    _git(repo_root, "commit", "-q", "-m", "second")
    second = _runner_git_info(source_dir)
    assert second["commit"] == _git(repo_root, "rev-parse", "HEAD")
    assert second["commit"] != first["commit"]
    assert second["dirty"] is False


def test_runner_git_info_reports_unavailable_git_as_null(tmp_path: Path) -> None:
    installed_dir = tmp_path / "site-packages" / "swe_runner" / "run" / "io"
    installed_dir.mkdir(parents=True)
    (installed_dir / "input_manifest.py").write_text("# installed copy\n", encoding="utf-8")
    info = _runner_git_info(installed_dir)
    assert info == {
        "repo_root": None,
        "commit": None,
        "dirty": None,
        "status_porcelain_sha256": None,
    }
