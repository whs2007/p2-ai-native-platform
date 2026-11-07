"""多角色 TDD：Planner / TestWriter / Executor / Verifier + TDDRunner（docs/07 §5.8）。"""
from __future__ import annotations

from specflow.agents.base import (
    AgentContext,
    AgentResult,
    BaseAgent,
    ScopeDecision,
    artifact_brief,
    artifact_section,
    check_scope,
    ensure_scope,
    normalize_rel_path,
    task_file_set,
)
from specflow.agents.executor import Executor
from specflow.agents.loop import TDDOutcome, TDDRunner
from specflow.agents.planner import Planner
from specflow.agents.test_writer import TestWriter
from specflow.agents.verifier import Verifier

__all__ = [
    "AgentContext",
    "AgentResult",
    "BaseAgent",
    "ScopeDecision",
    "Planner",
    "TestWriter",
    "Executor",
    "Verifier",
    "TDDRunner",
    "TDDOutcome",
    "artifact_brief",
    "artifact_section",
    "check_scope",
    "ensure_scope",
    "normalize_rel_path",
    "task_file_set",
]