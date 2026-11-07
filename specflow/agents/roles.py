"""四角色注册表 + 工具白名单（对应 docs/07 §5.8 与 README 口径）。

四个角色 = 3 个 LLM Agent（planner / test_writer / executor）+ 1 个工具型 Agent（verifier）。
白名单是**代码级硬约束**：未登记的工具调用直接 GateBlocked，而不是只写在文档里。
"""
from __future__ import annotations

from specflow.errors import GateBlocked

ROLE_KIND: dict[str, str] = {
    "planner": "llm",
    "test_writer": "llm",
    "executor": "llm",
    "verifier": "tool",
}

ROLE_TOOLS: dict[str, frozenset[str]] = {
    "planner": frozenset({"llm"}),
    "test_writer": frozenset({"llm", "write_file"}),
    "executor": frozenset({"llm", "write_file"}),
    "verifier": frozenset({"run_pytest"}),
}

ROLES: tuple[str, ...] = tuple(ROLE_KIND)


def allowed_tools(role: str) -> frozenset[str]:
    if role not in ROLE_TOOLS:
        raise GateBlocked(f"未知 Agent 角色：{role!r}（可用：{', '.join(ROLES)}）")
    return ROLE_TOOLS[role]


def assert_tool(role: str, tool: str) -> None:
    """角色调用未登记工具时硬阻断（exit 2）。"""
    tools = allowed_tools(role)
    if tool not in tools:
        raise GateBlocked(
            f"Agent {role} 调用未授权工具 {tool!r}（白名单：{', '.join(sorted(tools)) or '无'}）",
            reasons=[f"{role}.tools={tool}"],
            howto="在 specflow/agents/roles.py 登记该工具并说明边界后再使用。",
        )