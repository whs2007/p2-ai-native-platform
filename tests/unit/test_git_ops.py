"""Git 分支联动单测（默认关闭；开启后在临时 git 仓库验证创建/复用/非仓库报错）。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from specflow.errors import UsageError
from specflow.git_ops import ensure_feature_branch


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "tester")
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def test_create_branch(git_repo: Path) -> None:
    result = ensure_feature_branch(git_repo, "feature/demo")
    assert result["created"] is True and result["switched"] is True
    current = subprocess.run(
        ["git", "-C", str(git_repo), "branch", "--show-current"],
        text=True, capture_output=True, check=True,
    ).stdout.strip()
    assert current == "feature/demo"


def test_reuse_existing_branch(git_repo: Path) -> None:
    initial = subprocess.run(
        ["git", "-C", str(git_repo), "branch", "--show-current"],
        text=True, capture_output=True, check=True,
    ).stdout.strip()
    assert ensure_feature_branch(git_repo, "feature/demo")["created"] is True
    _git(git_repo, "switch", initial)
    second = ensure_feature_branch(git_repo, "feature/demo")
    assert second["created"] is False and second["switched"] is True


def test_non_git_repo_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Result:
        returncode = 128
        stdout = ""
        stderr = "fatal: not a git repository"

    monkeypatch.setattr("specflow.git_ops._git", lambda *_args, **_kwargs: _Result())
    with pytest.raises(UsageError):
        ensure_feature_branch(tmp_path, "feature/demo")


def test_empty_branch_rejected(git_repo: Path) -> None:
    with pytest.raises(UsageError):
        ensure_feature_branch(git_repo, "  ")