"""TestWriter：先写/更新测试文件，绝不写实现（docs/07 §5.8）。"""
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

__all__ = ["TestWriter", "TEST_SCHEMA"]

TEST_SCHEMA: Mapping[str, Any] = OUTPUT_SCHEMAS["tdd-tests"]
MIN_CASES = 3

_SYSTEM_INSTRUCTION = (
    "你是 TDD 的 Test Writer。根据任务与计划，先写出 pytest 测试文件（先于实现落盘）。"
    "只允许写 tests/ 下的 test_*.py；严禁写 src/ 或任何实现文件。"
    "用例至少 3 条，必须覆盖任务 acceptance[] 中的正向与边界场景；"
    "测试必须能被 pytest 直接收集，不得 mock 掉被测函数本身。\n"
    "输出字段：{\"files\": [{\"path\": \"tests/test_x.py\", \"content\": \"...\"}], \"cases\": 3}"
)


class TestWriter(BaseAgent):
    """测试先行角色：只写 tests/test_*.py。"""

    # 类名以 Test 开头，但它是产品类而不是 pytest 用例；避免被测试收集器误认。
    __test__ = False

    role = "test_writer"
    scenario = "tdd-tests"
    schema = TEST_SCHEMA

    def build_messages(self, ctx: AgentContext) -> list[dict[str, str]]:
        return self._messages(_SYSTEM_INSTRUCTION, self._context_payload(ctx))

    # ------------------------------------------------------------------ #
    def write(self, ctx: AgentContext) -> AgentResult:
        assert_tool("test_writer", "llm")
        result = self.invoke(ctx)
        self._validate(result.data)
        return result

    def apply(self, ctx: AgentContext, result: AgentResult) -> list[Path]:
        """把测试文件写盘；越界/写实现/用例不足 → GateBlocked。"""
        assert_tool("test_writer", "write_file")
        data = result.data or {}
        files = self._validate(data)
        prepared: list[tuple[Path, str]] = []
        for item in files:
            raw_path = str(item["path"])
            content = str(item["content"])
            rel = ensure_scope(raw_path, ctx=ctx, role="test_writer")
            prepared.append((ctx.project_dir / rel, content))
        written: list[Path] = []
        for target, content in prepared:
            atomic_write_text(target, content)
            written.append(target)
        return written

    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        cases = data.get("cases")
        if not isinstance(cases, int) or isinstance(cases, bool) or cases < MIN_CASES:
            raise GateBlocked(
                f"TestWriter 只产出 {cases!r} 条用例，少于最低要求 {MIN_CASES} 条",
                howto="补齐覆盖 acceptance[] 的正向与边界用例后重试。",
            )
        files = data.get("files")
        if not isinstance(files, list) or not files:
            raise GateBlocked("TestWriter 未产出任何测试文件")
        for item in files:
            if not isinstance(item, Mapping):
                raise GateBlocked("TestWriter 的 files[] 元素必须是 {path, content}")
            if not str(item.get("path") or "").strip():
                raise GateBlocked("TestWriter 的文件缺少 path")
            if not isinstance(item.get("content"), str) or not str(item["content"]).strip():
                raise GateBlocked("TestWriter 的文件 content 为空")
        return files