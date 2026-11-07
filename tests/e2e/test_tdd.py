"""端到端 TDD 验收（docs/07 §5.8 / 03 §6 / AC-15、AC-16、AC-17）。

用 `fixtures/demo-repo` 的可写副本 + MockLLM 跑 `TDDRunner`，断言：
- AC-15：`test_written` 事件早于 `impl_written`（测试先于实现）；
- AC-16：首轮失败 → 失败项回流 → 第 2 轮全绿（rounds=2，verdict=pass）；
- AC-17：`max_rounds=1` 时超限 → verdict=needs_human，保留测试文件与失败项。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from specflow.agents.loop import TDDRunner
from specflow.errors import SpecflowError
from specflow.llm import MockLLM

#: 07 §5.8「演示任务契约」：T1 声明文件、test_first、预估 1.5h。
T1_TASK: dict[str, Any] = {
    "id": "T1",
    "title": "实现拜访计划 CSV 导入",
    "desc": "实现 import_plans：表头校验、按 store_code 去重、跳过空行、日期解析。",
    "files": ["src/visit_plan/importer.py", "tests/test_importer.py"],
    "test_first": True,
    "estimate_h": 1.5,
    "acceptance": [
        "pytest -q tests/test_importer.py 全绿",
        "重复 store_code 只保留首次出现",
        "空白行与纯分隔行被跳过",
        "缺表头/空文件抛 ValueError",
    ],
}

_IMPL_PATH = Path("src") / "visit_plan" / "importer.py"
_TEST_PATH = Path("tests") / "test_importer.py"


def _read_events(settings) -> list[dict[str, Any]]:
    path = Path(settings.artifacts_dir) / "tdd-report.jsonl"
    assert path.exists(), "tdd-report.jsonl 未生成"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _event_pairs(events: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(event["role"], event["event"]) for event in events]


# ---------------------------------------------------------------------- #
# AC-15 / AC-16：测试先于实现 + 首轮失败回流后第 2 轮 pass
# ---------------------------------------------------------------------- #
def test_tdd_two_rounds_pass_end_to_end(repo: Path, settings):
    settings.ensure_spec_dirs()
    runner = TDDRunner(settings, None, None, MockLLM(), None)

    outcome = runner.run_task(T1_TASK)

    # AC-16：首轮失败回流，第 2 轮 pass
    assert outcome.verdict == "pass"
    assert outcome.rounds == 2
    assert outcome.passed == 7
    assert outcome.total == 7
    assert outcome.failure_items == []
    assert outcome.task_id == "T1"
    assert outcome.test_files == ["tests/test_importer.py"]
    assert outcome.impl_files == ["src/visit_plan/importer.py"]
    assert outcome.plan is not None and 1 <= len(outcome.plan["steps"]) <= 8

    # AC-15：测试先于实现（事件顺序 + 文件真实落盘）
    pairs = _event_pairs(outcome.events)
    assert pairs == [
        ("planner", "plan_ready"),
        ("test_writer", "test_written"),
        ("executor", "impl_written"),
        ("verifier", "fail"),
        ("planner", "plan_ready"),
        ("test_writer", "test_written"),
        ("executor", "impl_written"),
        ("verifier", "pass"),
    ]
    first_test = pairs.index(("test_writer", "test_written"))
    first_impl = pairs.index(("executor", "impl_written"))
    assert first_test < first_impl

    # 落盘顺序也以 jsonl 为证（runner 渲染报告时读它）
    records = _read_events(settings)
    assert _event_pairs(records) == pairs
    jsonl_test = _event_pairs(records).index(("test_writer", "test_written"))
    jsonl_impl = _event_pairs(records).index(("executor", "impl_written"))
    assert jsonl_test < jsonl_impl
    assert records[0]["stage"] == "S5"
    assert records[0]["round"] == 1

    # 验证事件带真实 pytest 计数：首轮 3 passed / 5 total，第 2 轮 7 / 7
    fail_event = next(event for event in records if event["event"] == "fail")
    assert fail_event["passed"] == 3
    assert fail_event["total"] == 5
    pass_event = next(event for event in records if event["event"] == "pass")
    assert pass_event["passed"] == 7 and pass_event["total"] == 7

    # 代码真被写进声明范围：测试 7 条、实现含去重修复
    test_file = repo / _TEST_PATH
    impl_file = repo / _IMPL_PATH
    assert test_file.exists() and impl_file.exists()
    test_content = test_file.read_text(encoding="utf-8")
    assert test_content.count("def test_") == 7
    impl_content = impl_file.read_text(encoding="utf-8")
    assert "seen" in impl_content
    assert "NotImplementedError" not in impl_content


# ---------------------------------------------------------------------- #
# AC-17：超过 max_rounds → needs_human 且保留测试
# ---------------------------------------------------------------------- #
def test_tdd_exceeding_max_rounds_needs_human(repo: Path, settings):
    settings.ensure_spec_dirs()
    runner = TDDRunner(settings, None, None, MockLLM(), None)

    outcome = runner.run_task(T1_TASK, max_rounds=1)

    assert outcome.verdict == "needs_human"
    assert outcome.rounds == 1
    assert outcome.passed == 3
    assert outcome.total == 5
    assert len(outcome.failure_items) == 2
    for item in outcome.failure_items:
        assert set(item) == {"case", "error", "action"}
        assert item["case"] and item["error"] and item["action"]

    # 超限：保留测试与首轮实现现场，供人工接管
    test_file = repo / _TEST_PATH
    impl_file = repo / _IMPL_PATH
    assert test_file.exists(), "needs_human 时必须保留测试文件"
    assert impl_file.exists(), "needs_human 时必须保留实现现场"
    assert "seen" not in impl_file.read_text(encoding="utf-8")

    pairs = _event_pairs(outcome.events)
    assert pairs[-1] == ("loop", "needs_human")
    assert ("verifier", "fail") in pairs
    assert ("verifier", "pass") not in pairs
    records = _read_events(settings)
    assert records[-1]["event"] == "needs_human"
    assert records[-1]["rounds"] == 1


# ---------------------------------------------------------------------- #
# 非法任务在计划阶段就被拒绝（不写任何文件）
# ---------------------------------------------------------------------- #
def test_tdd_rejects_task_without_test_first(repo: Path, settings):
    settings.ensure_spec_dirs()
    runner = TDDRunner(settings, None, None, MockLLM(), None)
    bad_task = dict(T1_TASK)
    bad_task["test_first"] = False

    with pytest.raises(SpecflowError, match="test_first"):
        runner.run_task(bad_task)

    assert not (repo / _TEST_PATH).exists()
    original_impl = (repo / _IMPL_PATH).read_text(encoding="utf-8")
    assert "NotImplementedError" in original_impl
    assert not (Path(settings.artifacts_dir) / "tdd-report.jsonl").exists()


# ---------------------------------------------------------------------- #
# run() 串行处理多任务
# ---------------------------------------------------------------------- #
def test_tdd_run_returns_one_outcome_per_task(repo: Path, settings):
    settings.ensure_spec_dirs()
    runner = TDDRunner(settings, None, None, MockLLM(), None)
    outcomes = runner.run([T1_TASK])
    assert len(outcomes) == 1
    assert outcomes[0].task_id == "T1"
    assert outcomes[0].verdict == "pass"


# ---------------------------------------------------------------------- #
# 与 T1 ArtifactStore / StateStore / Indexer 的集成
# ---------------------------------------------------------------------- #
def test_tdd_saves_plan_artifact_and_state(repo: Path, settings):
    from specflow.artifacts import schema as artifact_schema
    from specflow.artifacts.indexer import Indexer
    from specflow.artifacts.store import ArtifactStore
    from specflow.engine.state import StateStore

    settings.ensure_spec_dirs()
    store = ArtifactStore(settings)
    state_store = StateStore(settings.state_path)
    indexer = Indexer(settings)
    runner = TDDRunner(settings, store, state_store, MockLLM(), indexer)

    outcome = runner.run_task(T1_TASK, max_rounds=1)

    assert outcome.verdict == "needs_human"
    plan = store.load("tdd-plan.md")
    assert artifact_schema.validate(plan) == []
    assert plan.meta["task_id"] == "T1"
    assert plan.meta["round"] == 1
    # tdd-report.md 是 /add 的**多任务汇总产物**，由 engine.runner 保存；
    # TDDRunner 只负责事件流（tdd-report.jsonl）与 tdd-plan.md，避免双写。
    assert store.load_or_none("tdd-report.md") is None
    assert (Path(settings.artifacts_dir) / "tdd-report.jsonl").exists()

    # 超限时把 needs_human 落到 state.json
    assert state_store.load().needs_human is True

    # 索引记录交付代码/测试文件（旁路能力，失败不影响主流程）
    index = indexer.load()
    assert "src/visit_plan/importer.py" in index.get("files", {})
    assert "tests/test_importer.py" in index.get("files", {})