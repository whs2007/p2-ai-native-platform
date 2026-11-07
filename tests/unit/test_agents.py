"""四角色 Agent 单测（docs/07 §5.8）：Planner 计划校验、TestWriter 禁写实现、Executor 范围检查、Verifier 真跑 pytest。"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from specflow.agents import (
    AgentContext,
    Planner,
    TestWriter,
    Executor,
    Verifier,
)
from specflow.agents.base import (
    BaseAgent,
    check_scope,
    normalize_rel_path,
)
from specflow.errors import GateBlocked, SpecflowError, UsageError
from specflow.llm import MockLLM
from specflow.llm.base import LLMResult
from specflow.output_schemas import OUTPUT_SCHEMAS

MODELS_SRC = '''from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass
class Plan:
    store_code: str
    store_name: str
    plan_date: date
    owner: str
'''

DEMO_TASK: dict[str, Any] = {
    "id": "T1",
    "title": "实现拜访计划 CSV 导入",
    "desc": "实现 import_plans：表头校验、去重、空行跳过、日期解析。",
    "files": ["src/visit_plan/importer.py", "tests/test_importer.py"],
    "test_first": True,
    "estimate_h": 1.5,
    "acceptance": ["pytest 全绿", "重复 store_code 只保留首次"],
}


# ---------------------------------------------------------------------- #
# 帮助函数
# ---------------------------------------------------------------------- #
def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src" / "visit_plan").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "visit_plan" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "visit_plan" / "models.py").write_text(MODELS_SRC, encoding="utf-8")
    return repo


def _ctx(repo: Path, **overrides: Any) -> AgentContext:
    task = copy.deepcopy(DEMO_TASK)
    task.update(overrides.pop("task", {}))
    return AgentContext(
        project_dir=repo,
        task=task,
        tasks=[task],
        design_brief=overrides.pop("design_brief", "方案摘要"),
        domain_rules=overrides.pop("domain_rules", "领域规则"),
        repo_brief=overrides.pop("repo_brief", "仓库摘要"),
        **overrides,
    )


class StubLLM:
    """返回固定 data 的 LLM 替身（用于构造越界/非法计划）。"""

    mode = "mock"

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls: list[dict[str, Any]] = []

    def complete(self, messages, *, schema=None, scenario=None, round=None, temperature=None):  # noqa: ANN001
        self.calls.append({"scenario": scenario, "round": round, "schema": schema})
        return LLMResult(
            text=json.dumps(self.data, ensure_ascii=False),
            data=copy.deepcopy(self.data),
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            model="stub-llm",
            mode="mock",
            elapsed_ms=0,
        )


# ---------------------------------------------------------------------- #
# Planner
# ---------------------------------------------------------------------- #
def test_planner_returns_steps_within_task_scope(tmp_path):
    repo = _make_repo(tmp_path)
    ctx = _ctx(repo)
    result = Planner(MockLLM()).plan(ctx)
    steps = result.data["steps"]
    assert 1 <= len(steps) <= 8
    declared = set(DEMO_TASK["files"])
    for step in steps:
        assert step["goal"]
        assert step["verify"]
        assert step["files"]
        assert set(step["files"]) <= declared
    assert result.model == "mock-llm"


def test_planner_rejects_task_without_test_first(tmp_path):
    repo = _make_repo(tmp_path)
    ctx = _ctx(repo, task={"test_first": False})
    with pytest.raises(SpecflowError, match="test_first"):
        Planner(MockLLM()).plan(ctx)


def test_planner_rejects_task_without_files(tmp_path):
    repo = _make_repo(tmp_path)
    ctx = _ctx(repo, task={"files": []})
    with pytest.raises(SpecflowError, match="files"):
        Planner(MockLLM()).plan(ctx)


def test_planner_rejects_out_of_scope_step(tmp_path):
    repo = _make_repo(tmp_path)
    ctx = _ctx(repo)
    plan = {
        "steps": [
            {
                "n": 1,
                "goal": "写实现",
                "files": ["src/other/secret.py"],
                "verify": "pytest",
            }
        ]
    }
    with pytest.raises(GateBlocked, match="越界"):
        Planner(StubLLM(plan)).plan(ctx)


def test_planner_rejects_more_than_eight_steps(tmp_path):
    repo = _make_repo(tmp_path)
    ctx = _ctx(repo)
    steps = [
        {"n": index, "goal": f"步骤 {index}", "files": ["tests/test_importer.py"], "verify": "pytest"}
        for index in range(1, 10)
    ]
    with pytest.raises(GateBlocked, match="超过上限"):
        Planner(StubLLM({"steps": steps})).plan(ctx)


def test_planner_rejects_step_without_verify(tmp_path):
    repo = _make_repo(tmp_path)
    ctx = _ctx(repo)
    plan = {"steps": [{"n": 1, "goal": "写测试", "files": ["tests/test_importer.py"]}]}
    with pytest.raises(GateBlocked, match="verify"):
        Planner(StubLLM(plan)).plan(ctx)


# ---------------------------------------------------------------------- #
# TestWriter
# ---------------------------------------------------------------------- #
def test_test_writer_writes_test_file_before_impl(tmp_path):
    repo = _make_repo(tmp_path)
    ctx = _ctx(repo)
    writer = TestWriter(MockLLM())
    result = writer.write(ctx)
    paths = writer.apply(ctx, result)
    assert result.data["cases"] >= 3
    assert paths and paths[0] == repo / "tests" / "test_importer.py"
    content = paths[0].read_text(encoding="utf-8")
    assert "def test_" in content
    assert not (repo / "src" / "visit_plan" / "importer.py").exists()


def test_test_writer_rejects_implementation_file(tmp_path):
    repo = _make_repo(tmp_path)
    ctx = _ctx(repo)
    result = TestWriter(StubLLM(
        {
            "files": [{"path": "src/visit_plan/importer.py", "content": "x = 1\n"}],
            "cases": 3,
        }
    )).write(ctx)
    with pytest.raises(GateBlocked, match="C-02"):
        TestWriter(StubLLM({})).apply(ctx, result)
    assert not (repo / "src" / "visit_plan" / "importer.py").exists()


def test_test_writer_rejects_too_few_cases(tmp_path):
    repo = _make_repo(tmp_path)
    ctx = _ctx(repo)
    bad = StubLLM(
        {
            "files": [{"path": "tests/test_importer.py", "content": "def test_a():\n    pass\n"}],
            "cases": 1,
        }
    )
    with pytest.raises(GateBlocked, match="少于"):
        TestWriter(bad).write(ctx)


def test_test_writer_rejects_out_of_scope_path(tmp_path):
    repo = _make_repo(tmp_path)
    ctx = _ctx(repo)
    result = TestWriter(StubLLM(
        {
            "files": [{"path": "docs/test_notes.py", "content": "def test_x():\n    pass\n"}],
            "cases": 3,
        }
    )).write(ctx)
    with pytest.raises(GateBlocked):
        TestWriter(StubLLM({})).apply(ctx, result)
    assert not (repo / "docs").exists()


# ---------------------------------------------------------------------- #
# Executor
# ---------------------------------------------------------------------- #
def test_executor_writes_impl_round1_and_round2_differs(tmp_path):
    repo = _make_repo(tmp_path)
    executor = Executor(MockLLM())

    ctx1 = _ctx(repo, round=1)
    result1 = executor.apply(ctx1)
    paths1 = executor.write(ctx1, result1)
    assert paths1 == [repo / "src" / "visit_plan" / "importer.py"]
    content1 = paths1[0].read_text(encoding="utf-8")
    assert "seen" not in content1

    ctx2 = _ctx(repo, round=2, failure_items=[{"case": "test_dedupe", "error": "x", "action": "修"}])
    result2 = executor.apply(ctx2)
    paths2 = executor.write(ctx2, result2)
    content2 = paths2[0].read_text(encoding="utf-8")
    assert "seen" in content2
    assert content1 != content2


def test_executor_rejects_out_of_scope_file(tmp_path):
    repo = _make_repo(tmp_path)
    ctx = _ctx(repo)
    result = Executor(StubLLM(
        {"files": [{"path": "src/other/impl.py", "content": "x = 1\n"}]}
    )).apply(ctx)
    with pytest.raises(GateBlocked, match="C-01"):
        Executor(MockLLM()).write(ctx, result)
    assert not (repo / "src" / "other").exists()


def test_executor_rejects_modifying_tests(tmp_path):
    repo = _make_repo(tmp_path)
    ctx = _ctx(repo)
    result = Executor(StubLLM(
        {"files": [{"path": "tests/test_importer.py", "content": "def test_x():\n    pass\n"}]}
    )).apply(ctx)
    with pytest.raises(GateBlocked, match="C-03"):
        Executor(MockLLM()).write(ctx, result)


def test_executor_apply_uses_round_for_mock_scenario(tmp_path):
    repo = _make_repo(tmp_path)
    llm = MockLLM()
    executor = Executor(llm)
    executor.apply(_ctx(repo, round=1))
    executor.apply(_ctx(repo, round=2))
    assert [call["scenario"] for call in llm.calls] == ["tdd-impl", "tdd-impl"]
    assert [call["round"] for call in llm.calls] == [1, 2]


# ---------------------------------------------------------------------- #
# 路径范围基础函数
# ---------------------------------------------------------------------- #
def test_normalize_rel_path_rejects_escape(tmp_path):
    repo = _make_repo(tmp_path)
    with pytest.raises(GateBlocked, match="越界"):
        normalize_rel_path("../outside.py", repo)
    with pytest.raises(GateBlocked, match="越界"):
        normalize_rel_path(str(tmp_path / "elsewhere" / "x.py"), repo)


def test_check_scope_rules_for_roles(tmp_path):
    repo = _make_repo(tmp_path)
    task = DEMO_TASK
    assert check_scope("src/visit_plan/importer.py", task=task, role="executor", project_dir=repo).severity == "allow"
    assert check_scope("docs/readme.md", task=task, role="executor", project_dir=repo).rule_id == "C-01"
    assert check_scope("src/visit_plan/importer.py", task=task, role="test_writer", project_dir=repo).rule_id == "C-02"
    assert check_scope("tests/test_importer.py", task=task, role="executor", project_dir=repo).rule_id == "C-03"


def test_agent_context_failure_items_are_independent():
    first = AgentContext(
        project_dir=Path("."), task={}, tasks=[], design_brief="", domain_rules="", repo_brief=""
    )
    second = AgentContext(
        project_dir=Path("."), task={}, tasks=[], design_brief="", domain_rules="", repo_brief=""
    )
    first.failure_items.append({"case": "x"})
    assert second.failure_items == []


def test_base_agent_requires_scenario(tmp_path):
    class _Silent(BaseAgent):
        def build_messages(self, ctx):  # noqa: ANN001
            return [{"role": "user", "content": "x"}]

    with pytest.raises(UsageError, match="scenario"):
        _Silent(MockLLM()).invoke(_ctx(_make_repo(tmp_path)))


# ---------------------------------------------------------------------- #
# Verifier（真跑子进程 pytest）
# ---------------------------------------------------------------------- #
def test_verifier_reports_pass(tmp_path):
    repo = _make_repo(tmp_path)
    test_file = repo / "tests" / "test_ok.py"
    test_file.write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    ctx = _ctx(repo)
    result = Verifier().run(ctx, [test_file])
    assert result["passed"] is True
    assert result["passed_count"] == 1
    assert result["total"] == 1
    assert result["failure_items"] == []


def test_verifier_reports_failure_items(tmp_path):
    repo = _make_repo(tmp_path)
    test_file = repo / "tests" / "test_bad.py"
    test_file.write_text("def test_bad():\n    assert 1 == 2\n", encoding="utf-8")
    ctx = _ctx(repo)
    result = Verifier().run(ctx, [test_file])
    assert result["passed"] is False
    assert result["passed_count"] == 0
    assert result["total"] == 1
    assert result["failure_items"]
    item = result["failure_items"][0]
    assert set(item) == {"case", "error", "action"}
    assert "test_bad" in item["case"]


def test_verifier_handles_timeout(tmp_path):
    repo = _make_repo(tmp_path)
    test_file = repo / "tests" / "test_ok.py"
    test_file.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    result = Verifier(timeout_s=0.01).run(_ctx(repo), [test_file])
    assert result["passed"] is False
    assert result["failure_items"][0]["case"] == "<pytest>"
    assert "timeout" in result["failure_items"][0]["error"].lower()


def test_verifier_command_uses_current_interpreter_and_no_cache():
    command = Verifier().command(["tests/test_x.py"])
    assert command[0] == sys.executable
    assert command[1:3] == ["-m", "pytest"]
    assert "-q" in command
    assert "-p" in command and "no:cacheprovider" in command
    assert command[-1] == "tests/test_x.py"


def test_verifier_rejects_missing_test_files(tmp_path):
    repo = _make_repo(tmp_path)
    result = Verifier().run(_ctx(repo), [])
    assert result["passed"] is False
    assert result["failure_items"][0]["case"] == "<pytest>"