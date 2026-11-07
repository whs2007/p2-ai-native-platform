"""门控纯函数：缺产物 / 未批准 / tampered / 人门 / 摘要（docs/07 §5.4、AC-05~10）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from specflow.artifacts.store import ArtifactStore
from specflow.config import Settings
from specflow.engine.gate import GateResult, check, check_artifact, summarize_upstream
from specflow.engine.stages import Requirement, Stage
from specflow.engine.state import State, StateStore
from specflow.util import estimate_tokens

DESIGN_APPENDIX = {
    "gate": "design",
    "options": [
        {"name": "标准库 csv", "pros": "零依赖", "cons": "手写去重"},
        {"name": "pandas", "pros": "一行读取", "cons": "重依赖"},
    ],
    "changes": [{"path": "src/visit_plan/importer.py", "change": "新增 import_plans"}],
    "interfaces": [{"name": "import_plans(path)", "detail": "返回 Plan 列表"}],
    "risks": [
        {"id": "RISK-01", "desc": "空行崩溃", "mitigation": "逐行 strip", "rollback": "回退上一版"},
        {"id": "RISK-02", "desc": "重复编码", "mitigation": "seen 去重", "rollback": "关闭导入开关"},
        {"id": "RISK-03", "desc": "日期格式", "mitigation": "fromisoformat", "rollback": "回退旧解析"},
    ],
}

PRD_APPENDIX = {
    "requirements": [
        {
            "id": "REQ-01",
            "title": "CSV 导入",
            "desc": "提供 import_plans。",
            "acceptance": ["合法 CSV 返回 Plan 列表"],
        }
    ]
}


def make_settings(tmp_path: Path) -> Settings:
    return Settings.load(
        project_dir=tmp_path,
        overrides={"LLM_MODE": "mock", "PLATFORM_MODE": "mock", "SPECFLOW_OWNER": "tester"},
    )


def make_store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(make_settings(tmp_path))


def _body(sections: list[str], appendix: dict) -> str:
    lines: list[str] = ["# 产物", ""]
    for index, title in enumerate(sections, 1):
        lines += [f"## {index} {title}", "", f"{title} 内容。", ""]
    lines += ["## 附录：结构化数据", "", "```json", json.dumps(appendix, ensure_ascii=False, indent=2), "```", ""]
    return "\n".join(lines)


def save_design(store: ArtifactStore, *, status: str = "draft", body: str | None = None):
    body = body or _body(["选型对比", "模块改动", "接口变更", "风险与回滚"], DESIGN_APPENDIX)
    return store.save(
        "design.md",
        body,
        meta={"gate": "design", "summary": "用事件流替代轮询，回滚靠开关"},
        status=status,
        stage="S3",
        owner="tester",
    )


def save_prd(store: ArtifactStore):
    body = _body(["背景", "需求条目", "验收标准", "范围外"], PRD_APPENDIX)
    return store.save(
        "prd.md",
        body,
        meta={"source": {"type": "file", "ref": "docs/prd/visit-import.md"}, "summary": "拜访计划批量导入"},
        stage="S1",
        owner="tester",
    )


def make_stage(
    *,
    requires: tuple[Requirement, ...] = (),
    human_gate: str | None = None,
    stage_id: str = "S4",
    command: str = "/plan-task",
    slug: str = "plan-task",
) -> Stage:
    return Stage(
        id=stage_id,
        command=command,
        slug=slug,
        title="任务拆解",
        requires=requires,
        produces=("tasks.md",),
        human_gate=human_gate,
        max_context_tokens=3000,
        skill_path=Path("skills/plan-task/SKILL.md"),
        next="S5",
    )


def approve_design(store: ArtifactStore, state: State):
    artifact = store.set_status("design.md", "approved", owner="reviewer")
    state.record_artifact("design.md", artifact.version, artifact.hash, artifact.status, "S3")
    state.record_gate("design", "approved", by="reviewer", note="选型可接受")
    return artifact


PLAN_TASK_STAGE = make_stage(
    requires=(Requirement("design.md", "approved"),),
    human_gate="design",
)


# ---------------------------------------------------------------------- #
# GateResult 基础
# ---------------------------------------------------------------------- #
def test_gate_result_defaults_and_ok() -> None:
    result = GateResult(passed=True)
    assert result.ok is True
    assert result.missing == [] and result.reasons == [] and result.howto == ""
    assert result.details == {}
    assert GateResult(passed=False).ok is False


# ---------------------------------------------------------------------- #
# 缺产物（AC-05）
# ---------------------------------------------------------------------- #
def test_missing_upstream_artifact_blocks(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    result = check(PLAN_TASK_STAGE, store, State())
    assert result.passed is False
    assert any("缺少上游产物 design.md" in item for item in result.missing)
    assert "design.md:missing" in result.reasons
    assert "tech-design" in result.howto
    assert result.details["design.md"]["required"] == "approved"


def test_root_docs_checked_in_project_root(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    stage = make_stage(requires=(Requirement("AGENTS.md"),), human_gate=None)
    result = check(stage, store, State())
    assert result.passed is False
    assert "AGENTS.md:missing" in result.reasons

    (tmp_path / "AGENTS.md").write_text("# AGENTS.md\n", encoding="utf-8")
    assert check(stage, store, State()).passed is True


# ---------------------------------------------------------------------- #
# 未批准 / 状态不符（AC-06）
# ---------------------------------------------------------------------- #
def test_draft_design_blocks_with_actionable_reason(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    save_design(store)
    result = check(PLAN_TASK_STAGE, store, State())
    assert result.passed is False
    assert "design.md:status=draft(需 approved)" in result.reasons
    assert any("需要 approved" in item for item in result.missing)
    assert "specflow gate approve design" in result.howto


def test_approved_design_passes(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    state = State()
    save_design(store)
    approve_design(store, state)
    result = check(PLAN_TASK_STAGE, store, state)
    assert result.passed is True, result.missing
    assert result.reasons == [] and result.howto == ""


def test_frontmatter_and_state_status_mismatch_blocks(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    save_design(store)
    state = State()
    artifact = store.set_status("design.md", "approved", owner="reviewer")
    # 只写 state.artifacts 为 draft，模拟「文件说 approved、状态机说 draft」
    state.record_artifact("design.md", artifact.version, artifact.hash, "draft", "S3")
    state.record_gate("design", "approved", by="reviewer")
    result = check(PLAN_TASK_STAGE, store, state)
    assert result.passed is False
    assert any("state-status=draft" in item for item in result.reasons)


# ---------------------------------------------------------------------- #
# hash 篡改（AC-10）
# ---------------------------------------------------------------------- #
def test_tampered_approved_artifact_blocks(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    state = State()
    original = save_design(store)
    approved = approve_design(store, state)

    store.save(
        "design.md",
        approved.body + "\n\n> 手工追加：顺手优化\n",
        meta=dict(approved.meta),
        status="approved",
        stage="S3",
    )
    result = check(PLAN_TASK_STAGE, store, state)
    assert result.passed is False
    assert "design.md:tampered" in result.reasons
    assert result.details["design.md"]["tampered"] is True
    assert "restore design.md" in result.howto
    assert original.hash != store.load("design.md").hash


def test_check_artifact_detects_tamper(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    state = State()
    save_design(store)
    approved = approve_design(store, state)
    assert check_artifact("design.md", store, state) == []

    store.save("design.md", approved.body + "\n加一行\n", meta=dict(approved.meta), status="approved")
    assert "design.md:tampered" in check_artifact("design.md", store, state)
    assert check_artifact("missing.md", store, state) == ["missing.md:missing"]


# ---------------------------------------------------------------------- #
# schema 校验
# ---------------------------------------------------------------------- #
def test_schema_violation_blocks(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    state = State()
    incomplete = _body(["选型对比", "模块改动", "风险与回滚"], DESIGN_APPENDIX)  # 缺「接口变更」
    artifact = save_design(store, status="approved", body=incomplete)
    state.record_artifact("design.md", artifact.version, artifact.hash, "approved", "S3")
    state.record_gate("design", "approved", by="reviewer")
    result = check(PLAN_TASK_STAGE, store, state)
    assert result.passed is False
    assert any("缺少必备章节" in item and "接口变更" in item for item in result.missing)
    assert "design.md:schema" in result.reasons


# ---------------------------------------------------------------------- #
# 人门（AC-07/08）
# ---------------------------------------------------------------------- #
def test_human_gate_pending_blocks(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    state = State()
    save_design(store)
    artifact = store.set_status("design.md", "approved", owner="reviewer")
    state.record_artifact("design.md", artifact.version, artifact.hash, "approved", "S3")
    # gates.design 仍为 pending

    result = check(PLAN_TASK_STAGE, store, state)
    assert result.passed is False
    assert any("design 尚未 approve（当前 pending）" in item for item in result.missing)
    assert "gate:design=pending(需 approved)" in result.reasons
    assert "specflow gate approve design" in result.howto
    assert result.details["gates"]["design"]["status"] == "pending"


def test_design_gate_checks_artifact_even_without_requires(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    stage = make_stage(requires=(), human_gate="design")
    result = check(stage, store, State())
    assert result.passed is False
    assert any("design.md 存在" in item for item in result.missing)


def test_agents_md_gate_is_not_blocking(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    stage = make_stage(requires=(), human_gate="agents-md", command="/archive", slug="archive", stage_id="S7")
    result = check(stage, store, State())
    assert result.passed is True
    assert result.details["gates"]["agents-md"]["blocking"] is False


def test_reject_then_redo_flow(tmp_path: Path) -> None:
    """打回 → 重新生成 → 重新批准（AC-08 的纯函数侧）。"""
    store = make_store(tmp_path)
    state = State()
    save_design(store)
    approve_design(store, state)
    assert check(PLAN_TASK_STAGE, store, state).passed is True

    # 打回：状态回 draft
    rejected = store.set_status("design.md", "draft", owner="reviewer")
    state.record_artifact("design.md", rejected.version, rejected.hash, "draft", "S3")
    state.record_gate("design", "rejected", by="reviewer", note="选型缺少压测数据")
    blocked = check(PLAN_TASK_STAGE, store, state)
    assert blocked.passed is False
    assert any("status=draft" in item for item in blocked.reasons)

    # 重新生成并批准：审批轮次推进（rejected → approved 触发 +1）
    save_design(store)
    artifact = approve_design(store, state)
    assert check(PLAN_TASK_STAGE, store, state).passed is True
    assert state.gates["design"].round == 1
    assert artifact.status == "approved"


# ---------------------------------------------------------------------- #
# 连续失败计数（AC-09）
# ---------------------------------------------------------------------- #
def test_gate_failure_counter_three_times(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    state = State()
    state_store = StateStore(tmp_path / ".spec" / "state.json")
    for expected in (1, 2, 3):
        result = check(PLAN_TASK_STAGE, store, state)
        assert result.passed is False
        assert state.record_gate_failure("plan-task") == expected
    state_store.save(state)
    assert StateStore(tmp_path / ".spec" / "state.json").load().gate_failures["plan-task"] == 3

    # 成功后清零
    save_design(store)
    approve_design(store, state)
    assert check(PLAN_TASK_STAGE, store, state).passed is True
    state.clear_gate_failures("plan-task")
    assert "plan-task" not in state.gate_failures


# ---------------------------------------------------------------------- #
# 摘要注入
# ---------------------------------------------------------------------- #
def test_summarize_upstream_reads_summary_and_anchors(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    save_prd(store)
    stage = make_stage(requires=(Requirement("prd.md"),), human_gate=None)
    text = summarize_upstream(stage, store, 300)
    assert "prd.md" in text
    assert "拜访计划批量导入" in text
    assert ".spec/artifacts/prd.md#1-背景" in text
    assert estimate_tokens(text) <= 300


def test_summarize_upstream_respects_budget(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    save_prd(store)
    stage = make_stage(requires=(Requirement("prd.md"),), human_gate=None)
    text = summarize_upstream(stage, store, 12)
    assert estimate_tokens(text) <= 12


def test_summarize_upstream_without_artifacts(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    stage = make_stage(requires=(Requirement("design.md"),), human_gate=None)
    assert summarize_upstream(stage, store, 100) == "（无上游产物摘要）"