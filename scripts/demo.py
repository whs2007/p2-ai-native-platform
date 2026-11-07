"""specflow 全流程演示：init → Day 0 → S1..S7 → 回灌 → 状态自检。

用法（在项目根执行）：
    python scripts/demo.py                 # auto：有 Key 走 live，否则 mock
    python scripts/demo.py --mode mock     # 离线、确定、≤90s
    python scripts/demo.py --mode live     # 真实模型（.env 需配置 Key）
    python scripts/demo.py --reset         # 从 fixtures/demo-repo 重新建演示仓库

演示仓库默认在 <项目根>/.demo/visit-import（不污染 fixtures/demo-repo）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "demo-repo"
DEFAULT_WORKDIR = ROOT / ".demo" / "visit-import"


class StepFailed(RuntimeError):
    pass


def build_env(mode: str, owner: str) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["LLM_MODE"] = mode
    env["PLATFORM_MODE"] = "mock"
    env["SPECFLOW_OWNER"] = owner
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run(workdir: Path, env: dict[str, str], argv: list[str], *, timeout: int = 900,
        expect: int = 0) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        [sys.executable, "-m", "specflow", *argv],
        cwd=str(workdir),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
    )
    if expect is not None and proc.returncode != expect:
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        raise StepFailed(
            f"命令失败：specflow {' '.join(argv)}\n退出码 {proc.returncode}（期望 {expect}）\n"
            f"stdout:\n{out}\nstderr:\n{err}"
        )
    return proc


def has_live_key() -> bool:
    """检查 .env 或进程环境里是否有非空 LLM_API_KEY。"""
    if (os.environ.get("LLM_API_KEY") or "").strip():
        return True
    for candidate in (Path.cwd() / ".env", ROOT / ".env"):
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "LLM_API_KEY" and value.strip().strip("'\"") not in ("", '""'):
                return True
    return False


def prepare_workdir(workdir: Path, reset: bool) -> None:
    if workdir.exists() and reset:
        shutil.rmtree(workdir)
    if not workdir.exists():
        shutil.copytree(FIXTURE, workdir)
    elif not (workdir / "pyproject.toml").exists():
        shutil.copytree(FIXTURE, workdir, dirs_exist_ok=True)


def hook_deny_demo(workdir: Path, env: dict[str, str], command: str) -> bool:
    payload = {
        "session_id": "ses_demo",
        "hook_event_name": "PreToolUse",
        "cwd": str(workdir),
        "tool_name": "Bash",
        "tool_input": {"command": command, "description": "演示危险命令"},
    }
    proc = subprocess.run(
        [sys.executable, "-m", "specflow", "hook", "PreToolUse"],
        input=json.dumps(payload, ensure_ascii=False),
        cwd=str(workdir),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=60,
    )
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return False
    output = data.get("hookSpecificOutput") or {}
    decision = output.get("permissionDecision")
    print(f"  → decision={decision} rule={(data.get('specflow') or {}).get('rule_id')}")
    print(f"  → reason：{output.get('permissionDecisionReason', '')[:160]}")
    return decision == "deny"


def print_step(index: int, title: str, proc: subprocess.CompletedProcess[str],
               elapsed: float) -> None:
    print(f"\n[{index:02d}] {title}  ({elapsed:.2f}s)")
    for line in (proc.stdout or "").strip().splitlines():
        print(f"    {line}")
    for line in (proc.stderr or "").strip().splitlines():
        print(f"    ! {line}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="specflow 全流程演示")
    parser.add_argument("--mode", choices=("auto", "mock", "live"), default="auto")
    parser.add_argument("--workdir", default=str(DEFAULT_WORKDIR))
    parser.add_argument("--reset", action="store_true", help="重建演示仓库")
    parser.add_argument("--owner", default="lin.xu")
    parser.add_argument("--skip-tdd", action="store_true", help="跳过 /add 与后续 QA（只演示到任务拆解）")
    parser.add_argument("--fixture", default=None, help="pipeline-fail / rpc-error，用于 QA 失败分支")
    args = parser.parse_args(argv)

    workdir = Path(args.workdir).resolve()
    prepare_workdir(workdir, args.reset)
    mode = args.mode
    env = build_env(mode, args.owner)
    if mode == "auto":
        has_key = has_live_key()
        mode = "live" if has_key else "mock"
        env["LLM_MODE"] = mode
    if mode == "live" and not has_live_key():
        print("live 模式需要模型配置：请复制 .env.example 为 .env 并填写 LLM_API_KEY / LLM_MODEL。")
        print("或改用离线演示：python scripts/demo.py --mode mock --reset")
        return 2
    print(f"演示仓库：{workdir}")
    print(f"LLM 模式：{mode} ｜ 平台：mock ｜ 操作人：{args.owner}")
    print("=" * 72)

    steps: list[tuple[str, list[str], bool, int | None]] = [
        ("初始化 .spec 骨架 / hooks.json / AGENTS.md", ["init"], False, 0),
        ("S0A 仓库画像 + 模板提取", ["run", "/project-init"], False, 0),
        ("S0B 领域知识", ["run", "/domain-input"], False, 0),
        ("S1 PRD 输入", ["run", "/prd-input", "docs/prd/visit-import.md"], False, 0),
        ("（演示门控）越级执行 /plan-task", ["run", "/plan-task"], True, 2),
        ("S2 综合探索", ["run", "/explore"], False, 0),
        ("S3 技术方案", ["run", "/tech-design"], False, 0),
        ("人工门控：批准 design", ["gate", "approve", "design", "--as", args.owner, "--note", "选型可接受"], False, 0),
        ("S4 任务拆解", ["run", "/plan-task"], False, 0),
    ]
    if not args.skip_tdd:
        if args.fixture:
            steps.extend(
                [
                    ("S5 TDD 四角色循环", ["run", "/add", "--task", "T1"], False, 0),
                    (f"（失败分支）QA {args.fixture}", ["run", "/qa-validate", "--fixture", args.fixture], False, 0),
                    ("（失败分支）尝试 /archive —— 应被门控阻断", ["run", "/archive"], True, 2),
                ]
            )
        else:
            steps.extend(
                [
                    ("S5 TDD 四角色循环", ["run", "/add", "--task", "T1"], False, 0),
                    ("S6 QA 三段证据", ["run", "/qa-validate"], False, 0),
                    ("S7 归档 + 候选规则", ["run", "/archive"], False, 0),
                    ("人工门控：回灌 AGENTS.md", ["gate", "approve", "agents-md", "--as", args.owner, "--note", "规则可接受"], False, 0),
                ]
            )
    steps.extend(
        [
            ("状态自检", ["status", "--check"], False, 0),
            ("环境自检", ["doctor"], False, 0),
        ]
    )

    timings: list[tuple[str, float]] = []
    started = time.perf_counter()
    for index, (title, command, is_gate_demo, expect) in enumerate(steps, start=1):
        step_start = time.perf_counter()
        try:
            proc = run(workdir, env, command, expect=expect)
        except StepFailed as exc:
            print(f"\n[{index:02d}] {title} 失败")
            print(str(exc))
            return 1
        elapsed = time.perf_counter() - step_start
        timings.append((title, elapsed))
        print_step(index, title, proc, elapsed)
        if is_gate_demo:
            if proc.returncode != 2:
                print(f"    ! 期望被门控阻断（exit 2），实际 {proc.returncode}")
                return 1
            print("    [OK] 已在门控层阻断，未调用模型")

    print("\n" + "=" * 72)
    print("危险命令拦截演示（PreToolUse）")
    ok = hook_deny_demo(workdir, env, "git reset --hard HEAD~3")
    print("\n" + "=" * 72)
    print("阶段耗时：")
    for title, elapsed in timings:
        print(f"  {elapsed:7.2f}s  {title}")
    total = time.perf_counter() - started
    print(f"  合计 {total:.2f}s（模式 {mode}）")
    if not ok:
        print("x PreToolUse 未按预期 deny")
        return 1
    print("\n演示完成。产物在 .spec/artifacts/，trace 在 .spec/traces/。")
    if mode == "mock":
        print(f"提示：mock 模式全流程应 ≤90s（本次 {total:.1f}s）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())