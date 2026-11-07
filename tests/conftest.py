"""共享测试夹具：临时演示仓库、配置、CLI 调用、hook 子进程调用。

约定（docs/07 §8）：测试统一在「fixtures/demo-repo 的副本」里跑，
不修改 fixture 本体；LLM 一律 mock，保证确定性与离线。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_REPO = PLATFORM_ROOT / "fixtures" / "demo-repo"


# ---------------------------------------------------------------------- #
# Windows 沙箱兼容：mode=0o700 的目录在本会话里创建后立即不可访问
# （WinError 5：tempfile.mkdtemp / pytest tmp_path / pytest cache 都用 0o700）。
# 这里把 mkdir 的 mode 统一降级为默认值；仅影响测试进程，不影响产品代码。
# ---------------------------------------------------------------------- #
def _install_mkdir_patch() -> None:
    if getattr(os, "_specflow_mkdir_patched", False):
        return
    real_os_mkdir = os.mkdir
    real_path_mkdir = Path.mkdir

    def _safe_os_mkdir(path, mode=0o777, *, dir_fd=None):
        if dir_fd is not None:
            return real_os_mkdir(path, 0o777, dir_fd=dir_fd)
        return real_os_mkdir(path, 0o777)

    def _safe_path_mkdir(self, mode=0o777, parents=False, exist_ok=False):
        return real_path_mkdir(self, 0o777, parents=parents, exist_ok=exist_ok)

    os.mkdir = _safe_os_mkdir  # type: ignore[assignment]
    Path.mkdir = _safe_path_mkdir  # type: ignore[method-assign]
    os._specflow_mkdir_patched = True  # type: ignore[attr-defined]


_install_mkdir_patch()


def pytest_configure(config: pytest.Config) -> None:
    """并行开发时多个 pytest 进程共用 %TEMP% 会互相清理 basetemp（WinError 5）。

    这里给每个进程一个**工作区内**的独立 basetemp：既避免并发冲突，
    又避开 Windows 沙箱对平台临时目录的访问限制。显式传 --basetemp 时不覆盖。
    """
    if getattr(config.option, "basetemp", None):
        return
    base = PLATFORM_ROOT / ".pytest-tmp" / f"p{os.getpid()}"
    base.mkdir(parents=True, exist_ok=True)
    config.option.basetemp = str(base)


def make_settings(repo: Path, **overrides: Any):
    """构造指向临时仓库的 mock 配置。"""
    from specflow.config import Settings

    base: dict[str, Any] = {
        "LLM_MODE": "mock",
        "PLATFORM_MODE": "mock",
        "SPECFLOW_OWNER": "tester",
        "LLM_CACHE": "0",
    }
    base.update(overrides)
    return Settings.load(project_dir=repo, overrides=base)


def hook_env(repo: Path, extra: Mapping[str, Any] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PLATFORM_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.update(
        {
            "SPECFLOW_PROJECT_DIR": str(repo),
            "LLM_MODE": "mock",
            "PLATFORM_MODE": "mock",
            "SPECFLOW_OWNER": "tester",
            "SPECFLOW_STRICT_HOOKS": "0",
        }
    )
    env.update({str(k): str(v) for k, v in (extra or {}).items()})
    return env


def run_hook(event: str, payload: Mapping[str, Any], repo: Path, *,
             extra_env: Mapping[str, Any] | None = None) -> subprocess.CompletedProcess[str]:
    """以真实子进程执行 `python -m specflow hook <Event>`，喂 stdin JSON。"""
    return subprocess.run(
        [sys.executable, "-m", "specflow", "hook", event],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        cwd=str(repo),
        env=hook_env(repo, extra_env),
        timeout=60,
    )


def run_cli(repo: Path, *argv: str, extra_env: Mapping[str, Any] | None = None
            ) -> subprocess.CompletedProcess[str]:
    """以真实子进程执行 `python -m specflow ...`（验收用）。"""
    return subprocess.run(
        [sys.executable, "-m", "specflow", *argv],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        cwd=str(repo),
        env=hook_env(repo, extra_env),
        timeout=300,
    )


@pytest.fixture(scope="session")
def platform_root() -> Path:
    return PLATFORM_ROOT


@pytest.fixture(scope="session")
def demo_repo_src() -> Path:
    if not FIXTURE_REPO.exists():
        pytest.skip("fixtures/demo-repo 尚未就绪")
    return FIXTURE_REPO


@pytest.fixture()
def repo(tmp_path: Path, demo_repo_src: Path) -> Path:
    """fixture 仓库的可写副本（不含 .spec，需要时先 init）。"""
    dest = tmp_path / "repo"
    shutil.copytree(demo_repo_src, dest)
    return dest


@pytest.fixture()
def settings(repo: Path):
    return make_settings(repo)


@pytest.fixture()
def inited_repo(repo: Path, settings):
    """已执行 `specflow init` 的临时仓库。"""
    from specflow.cli import main

    code = main(["init"], settings=settings)
    assert code == 0, "specflow init 失败"
    return repo


@pytest.fixture()
def cli_call(inited_repo: Path, settings):
    """进程内调用 CLI：cli_call(["status", "--json"]) → (exit_code, stdout)。"""
    from specflow.cli import main

    def _call(argv: list[str], *, settings_override=None) -> tuple[int, str]:
        return main(argv, settings=settings_override or settings), ""

    return _call


@pytest.fixture()
def hook_runner(inited_repo: Path) -> Callable[..., subprocess.CompletedProcess[str]]:
    def _run(event: str, payload: Mapping[str, Any], *, extra_env=None):
        payload = dict(payload)
        payload.setdefault("cwd", str(inited_repo))
        payload.setdefault("session_id", "ses_test")
        payload.setdefault("hook_event_name", event)
        return run_hook(event, payload, inited_repo, extra_env=extra_env)

    return _run


@pytest.fixture()
def hook_json(hook_runner) -> Callable[..., tuple[dict[str, Any], int]]:
    """解析 hook stdout JSON，返回 (payload, exit_code)。"""

    def _call(event: str, payload: Mapping[str, Any], *, extra_env=None):
        proc = hook_runner(event, payload, extra_env=extra_env)
        data = json.loads(proc.stdout) if proc.stdout.strip() else {}
        return data, proc.returncode

    return _call