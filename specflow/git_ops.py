"""可选 Git 分支联动：`/plan-task` 绑定 feature 后，在目标仓库创建/切换分支。

默认关闭（`SPECFLOW_GIT_BRANCH=0`）；只改分支，不做 commit/push/merge。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from specflow.errors import UsageError


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
    )


def ensure_feature_branch(repo: Path, branch: str) -> dict[str, object]:
    name = (branch or "").strip()
    if not name:
        raise UsageError("分支名为空，无法创建/切换")
    probe = _git(repo, "rev-parse", "--is-inside-work-tree")
    if probe.returncode != 0:
        raise UsageError(f"项目目录不是 git 仓库：{repo}（请关闭 SPECFLOW_GIT_BRANCH 或先 git init）")
    current = _git(repo, "branch", "--show-current").stdout.strip()
    if current == name:
        return {"branch": name, "created": False, "switched": False, "previous": current}
    exists = _git(repo, "rev-parse", "--verify", name).returncode == 0
    command = ("switch", name) if exists else ("switch", "-c", name)
    proc = _git(repo, *command)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[:200]
        raise UsageError(f"git 切换分支失败：{name}；{detail}")
    return {"branch": name, "created": not exists, "switched": True, "previous": current}