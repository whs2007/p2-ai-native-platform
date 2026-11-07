"""四角色注册表与工具白名单单测。"""

from __future__ import annotations

import pytest

from specflow.agents.roles import ROLES, ROLE_KIND, ROLE_TOOLS, allowed_tools, assert_tool
from specflow.errors import GateBlocked


def test_four_roles_registered() -> None:
    assert ROLES == ("planner", "test_writer", "executor", "verifier")
    assert ROLE_KIND["verifier"] == "tool"
    assert all(ROLE_KIND[name] == "llm" for name in ("planner", "test_writer", "executor"))


def test_verifier_has_only_pytest_tool() -> None:
    assert allowed_tools("verifier") == frozenset({"run_pytest"})
    with pytest.raises(GateBlocked):
        assert_tool("verifier", "write_file")


def test_planner_cannot_write_files() -> None:
    with pytest.raises(GateBlocked):
        assert_tool("planner", "write_file")
    assert_tool("test_writer", "write_file")