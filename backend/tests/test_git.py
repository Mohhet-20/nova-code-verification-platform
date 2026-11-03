from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.services import git

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def test_non_git_directory(tmp_path: Path) -> None:
    assert git.inspect_repository(tmp_path, 5, 10_000) == git.GitInfo(False)


def test_git_repository_info(git_repo: Path) -> None:
    info = git.inspect_repository(git_repo, 5, 100_000)
    assert info.is_git and info.branch == "main" and info.head and len(info.head) == 40
    assert info.dirty_count == 0
    (git_repo / "new.txt").write_text("x")
    assert git.inspect_repository(git_repo, 5, 100_000).dirty_count == 1


def test_output_is_bounded(git_repo: Path) -> None:
    res = git.run_git(git_repo, ["ls-files"], 5, 10)
    assert res.truncated and len(res.stdout) <= 10


def test_arguments_are_not_shell_interpreted(git_repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    res = git.run_git(git_repo, ["log", f"--format=%H; touch {marker}"], 5, 10_000)
    assert res.ok and not marker.exists()


def test_timeout_is_handled(git_repo: Path) -> None:
    assert git.run_git(git_repo, ["status"], 1e-6, 1000).ok is False
