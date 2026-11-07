"""Executor：在任务声明范围内写实现（docs/07 §5.8）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from specflow.agents.base import (
    AgentContext,
    AgentResult,
    BaseAgent,
    ensure_scope,
)
from specflow.agents.roles import assert_tool
from specflow.errors import GateBlocked
from specflow.output_schemas import OUTPUT_SCHEMAS
from specflow.util import atomic_write_text

__all__ = ["Executor", "IMPL_SCHEMA"]

IMPL_SCHEMA: Mapping[str, Any] = OUTPUT_SCHEMAS["tdd-impl"]

_SYSTEM_INSTRUCTION = (
    "你是 TDD 的 Executor。测试已经先落盘；现在只写让测试变绿的最小实现。"
    "严禁修改、删除、重命名 tests/ 下的测试文件；只写 tasks.md 任务 files[] 内的实现文件。"
    "如果上下文里有 failure_items，逐条针对失败原因修复，不要重写无关代码。\n"
    "输出字段：{\"files\": [{\"path\": \"src/xxx.py\", \"content\": \"...\"}]}"
)


class Executor(BaseAgent):
    role = "executor"
    scenario = "tdd-impl"
    schema = IMPL_SCHEMA

    def build_messages(self, ctx: AgentContext) -> list[dict[str, str]]:
        return self._messages(_SYSTEM_INSTRUCTION, self._context_payload(ctx))

    # ------------------------------------------------------------------ #
    def apply(self, ctx: AgentContext) -> AgentResult:
        assert_tool("executor", "llm")
        result = self.invoke(ctx, scenario="tdd-impl", round=ctx.round)
        files = result.data.get("files")
        if not isinstance(files, list) or not files:
            raise GateBlocked("Executor 未产出任何实现文件")
        return result

    def write(self, ctx: AgentContext, result: AgentResult) -> list[Path]:
        """走 C-01/C-03 范围检查后写盘，越界抛 GateBlocked。"""
        assert_tool("executor", "write_file")
        files = result.data.get("files")
        if not isinstance(files, list) or not files:
            raise GateBlocked("Executor 未产出任何实现文件")
        prepared: list[tuple[Path, str, str]] = []
        for item in files:
            if not isinstance(item, Mapping):
                raise GateBlocked("Executor 的 files[] 元素必须是 {path, content}")
            raw_path = str(item.get("path") or "").strip()
            content = item.get("content")
            if not raw_path:
                raise GateBlocked("Executor 的文件缺少 path")
            if not isinstance(content, str) or not content.strip():
                raise GateBlocked(f"Executor 的文件 {raw_path} content 为空")
            rel = ensure_scope(raw_path, ctx=ctx, role="executor")
            prepared.append((ctx.project_dir / rel, content, rel))
        written: list[Path] = []
        for target, content, _rel in prepared:
            atomic_write_text(target, content)
            written.append(target)
        return written