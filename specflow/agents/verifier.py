"""Verifier：真跑 pytest，解析通过/失败并产出 failure_items（docs/07 §5.8）。

命令固定为
`[sys.executable, "-m", "pytest", "-q", "-o", "addopts=", "-p", "no:cacheprovider", *rel_test_files]`，
cwd=项目根，不做任何 mock —— TDD 的“自证”必须来自真实测试进程。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from specflow.agents.base import AgentContext
from specflow.agents.roles import assert_tool
from specflow.util import rel_posix

__all__ = ["Verifier", "parse_pytest_output"]

_COUNT_PATTERNS = {
    "passed": re.compile(r"(\d+)\s+passed\b"),
    "failed": re.compile(r"(\d+)\s+failed\b"),
    "error": re.compile(r"(\d+)\s+errors?\b"),
}
_FAILED_LINE = re.compile(r"^FAILED\s+(\S+)(?:\s+-\s+(.*))?$", re.MULTILINE)
_ERROR_LINE = re.compile(r"^ERROR\s+(\S+)(?:\s+-\s+(.*))?$", re.MULTILINE)
_E_LINE = re.compile(r"^E\s+(.+)$", re.MULTILINE)

_FAIL_ACTION = "按该用例的断言修正实现后重跑 pytest"
_ERROR_ACTION = "先修复导入/收集错误（模块路径、语法、依赖）后重跑 pytest"


def _count(kind: str, output: str) -> int:
    match = _COUNT_PATTERNS[kind].search(output)
    return int(match.group(1)) if match else 0


def parse_pytest_output(output: str, returncode: int | None = None) -> dict[str, Any]:
    """把 `pytest -q` 输出解析为统一结果（可直接用于 tdd-report）。"""
    passed_count = _count("passed", output)
    failed_count = _count("failed", output)
    error_count = _count("error", output)
    failure_items: list[dict[str, str]] = []

    for match in _FAILED_LINE.finditer(output):
        case, detail = match.group(1), match.group(2)
        failure_items.append(
            {
                "case": case,
                "error": (detail or "测试失败").strip()[:500],
                "action": _FAIL_ACTION,
            }
        )
    for match in _ERROR_LINE.finditer(output):
        case, detail = match.group(1), match.group(2)
        failure_items.append(
            {
                "case": case,
                "error": (detail or "收集/导入错误").strip()[:500],
                "action": _ERROR_ACTION,
            }
        )
    if not failure_items and (failed_count or error_count):
        snippets = [line.strip() for line in _E_LINE.findall(output)][:5]
        detail = "；".join(snippets) if snippets else "pytest 报告失败"
        failure_items.append(
            {"case": "<pytest>", "error": detail[:500], "action": _FAIL_ACTION}
        )

    passed = returncode == 0 and not failed_count and not error_count and passed_count > 0
    total = passed_count + failed_count + error_count
    if not passed and total == 0 and returncode not in (0, None):
        failure_items.append(
            {
                "case": "<pytest>",
                "error": f"未收集到可执行用例（pytest 退出码 {returncode}）",
                "action": "检查测试文件是否能被 pytest 收集",
            }
        )
    return {
        "passed": passed,
        "passed_count": passed_count,
        "total": total,
        "failure_items": failure_items,
        "output": output,
    }


class Verifier:
    """子进程跑 pytest；超时/失败都转成 failure_items，不抛异常。"""

    def __init__(self, timeout_s: float = 180.0) -> None:
        self.timeout_s = float(timeout_s)

    def command(self, test_files: Sequence[Path | str]) -> list[str]:
        # `-o addopts=`：目标仓库可能在 pyproject 里配置了 addopts（例如 demo-repo 的
        # `-q`），叠加命令行 `-q` 会变成 -qq 并把 “x passed, y failed” 汇总行吞掉，
        # 导致无法解析通过数；这里显式清空仓库级 addopts，只保留本命令的参数。
        # `-p no:cacheprovider`：目标仓库可能位于受限/只读环境，关闭 .pytest_cache 创建。
        # 两者都不改变用例语义。
        return [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            *[str(item) for item in test_files],
        ]

    def run(self, ctx: AgentContext, test_files: Sequence[Path]) -> dict[str, Any]:
        assert_tool("verifier", "run_pytest")
        project_dir = Path(ctx.project_dir)
        rels = [rel_posix(Path(path), project_dir) for path in test_files]
        if not rels:
            return {
                "passed": False,
                "passed_count": 0,
                "total": 0,
                "failure_items": [
                    {
                        "case": "<pytest>",
                        "error": "没有测试文件可执行",
                        "action": "先由 TestWriter 落盘测试文件",
                    }
                ],
                "output": "",
            }
        command = self.command(rels)
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        try:
            proc = subprocess.run(
                command,
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return {
                "passed": False,
                "passed_count": 0,
                "total": 0,
                "failure_items": [
                    {
                        "case": "<pytest>",
                        "error": f"timeout（>{self.timeout_s:.0f}s）",
                        "action": "排查死循环或缩小测试范围后重跑",
                    }
                ],
                "output": f"pytest 超时：{self.timeout_s}s",
            }
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        output = stdout if not stderr.strip() else f"{stdout}\n{stderr}"
        result = parse_pytest_output(output, proc.returncode)
        if not result["passed"] and not result["failure_items"]:
            result["failure_items"].append(
                {
                    "case": "<pytest>",
                    "error": f"pytest 退出码 {proc.returncode}",
                    "action": "查看 pytest 输出后修复",
                }
            )
        result["output"] = output[-4000:]
        return result