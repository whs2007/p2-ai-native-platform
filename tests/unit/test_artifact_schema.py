"""产物结构契约：9 类产物的必备 meta / 章节 / 附录 / 结构断言（docs/07 §4.3）。"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from specflow.artifacts import schema
from specflow.artifacts.store import Artifact, ArtifactStore
from specflow.config import Settings

EXPECTED_NAMES = {
    "repo-profile.md",
    "prd.md",
    "exploration.md",
    "design.md",
    "tasks.md",
    "tdd-plan.md",
    "tdd-report.md",
    "qa-report.md",
    "archive-record.md",
}

_DEFAULT = object()


def _settings(tmp_path: Path) -> Settings:
    return Settings.load(
        project_dir=tmp_path,
        overrides={"LLM_MODE": "mock", "PLATFORM_MODE": "mock", "SPECFLOW_OWNER": "tester"},
    )


def valid_appendix(name: str) -> dict[str, Any]:
    return {
        "repo-profile.md": {
            "language": "Python",
            "package_manager": "pip",
            "test_command": "python -m pytest -q",
            "entry_points": ["src/visit_plan/__main__.py"],
            "convention_files": ["pyproject.toml"],
            "commands": [
                {"purpose": "测试", "command": "python -m pytest -q"},
                {"purpose": "安装", "command": "pip install -e ."},
                {"purpose": "Lint", "command": "ruff check src tests"},
            ],
        },
        "prd.md": {
            "requirements": [
                {
                    "id": "REQ-01",
                    "title": "CSV 导入",
                    "desc": "提供 import_plans。",
                    "acceptance": ["合法 CSV 返回 Plan 列表"],
                }
            ]
        },
        "exploration.md": {
            "modules": [{"path": "src/visit_plan/importer.py", "purpose": "导入入口"}],
            "constraints": ["仅标准库"],
            "unknowns": [{"item": "去重保留首次还是最后一次", "status": "待确认"}],
            "deps": [],
        },
        "design.md": {
            "gate": "design",
            "options": [
                {"name": "标准库 csv", "pros": "零依赖", "cons": "手写去重"},
                {"name": "pandas", "pros": "一行读取", "cons": "重依赖"},
            ],
            "changes": [{"path": "src/visit_plan/importer.py", "change": "新增 import_plans"}],
            "interfaces": [{"name": "import_plans(path)", "detail": "返回 Plan 列表"}],
            "risks": [
                {"id": "RISK-01", "desc": "空行崩溃", "mitigation": "逐行 strip", "rollback": "回退上一版"},
                {"id": "RISK-02", "desc": "重复编码", "mitigation": "seen 去重", "rollback": "关闭导入入口"},
                {"id": "RISK-03", "desc": "日期格式", "mitigation": "fromisoformat", "rollback": "回退旧解析"},
            ],
        },
        "tasks.md": {
            "feature_id": "FEAT-1024",
            "tasks": [
                {
                    "id": "T1",
                    "title": "实现 CSV 导入",
                    "files": ["src/visit_plan/importer.py", "tests/test_importer.py"],
                    "test_first": True,
                    "estimate_h": 1.5,
                    "acceptance": ["pytest 全绿"],
                }
            ],
            "order": ["T1"],
        },
        "tdd-plan.md": {
            "steps": [
                {
                    "n": 1,
                    "goal": "确认任务范围",
                    "files": ["tests/test_importer.py"],
                    "verify": "pytest -q tests/test_importer.py",
                }
            ]
        },
        "tdd-report.md": {
            "rounds": 2,
            "verdict": "pass",
            "needs_human": False,
            "tasks": [
                {"task_id": "T1", "rounds": 2, "verdict": "pass", "passed": 5, "total": 5, "failures": []}
            ],
        },
        "qa-report.md": {
            "verdict": "pass",
            "evidence": [
                {"type": "unit", "status": "passed", "detail": "5 passed"},
                {"type": "pipeline", "status": "passed", "detail": "RUN-1024"},
                {"type": "rpc", "status": "passed", "detail": "cases=5"},
            ],
        },
        "archive-record.md": {
            "rules_proposed": [
                {
                    "id": "R-07",
                    "text": "CSV 导入按 store_code 去重并保留首次出现",
                    "source": "REQ-02",
                    "criteria": "同一唯一键出现多次只保留第一条",
                    "counterexample": "不去重导致重复打卡",
                }
            ],
            "changes": ["新增 importer"],
            "reusable": ["CSV 三件套"],
        },
    }[name]


def valid_meta(name: str) -> dict[str, Any]:
    return {
        "prd.md": {"source": {"type": "file", "ref": "docs/prd/visit-import.md"}},
        "design.md": {"gate": "design"},
        "tasks.md": {"feature_id": "FEAT-1024"},
        "tdd-plan.md": {"task_id": "T1", "round": 1},
        "tdd-report.md": {"task_id": "T1", "rounds": 2, "verdict": "pass", "needs_human": False},
        "qa-report.md": {"verdict": "pass"},
    }.get(name, {})


def render_body(sections: tuple[str, ...], appendix: dict[str, Any] | None) -> str:
    lines: list[str] = ["# 产物", ""]
    for index, title in enumerate(sections, 1):
        lines += [f"## {index} {title}", "", f"{title} 内容。", ""]
    if appendix is not None:
        lines += [
            "## 附录：结构化数据",
            "",
            "```json",
            json.dumps(appendix, ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    return "\n".join(lines)


def build(
    tmp_path: Path,
    name: str,
    *,
    appendix: Any = _DEFAULT,
    sections: Any = _DEFAULT,
    meta: Any = _DEFAULT,
    raw_body: str | None = None,
) -> Artifact:
    store = ArtifactStore(_settings(tmp_path))
    artifact_schema = schema.ARTIFACT_SCHEMAS[name]
    if raw_body is None:
        app = valid_appendix(name) if appendix is _DEFAULT else appendix
        secs = artifact_schema.required_sections if sections is _DEFAULT else sections
        raw_body = render_body(secs, app)
    extra = valid_meta(name)
    if meta is not _DEFAULT:
        extra.update(meta)
    return store.save(name, raw_body, meta=extra, stage="S3", owner="tester")


# ---------------------------------------------------------------------- #
# 注册表
# ---------------------------------------------------------------------- #
def test_registry_covers_all_nine_artifacts() -> None:
    assert set(schema.ARTIFACT_SCHEMAS) == EXPECTED_NAMES
    assert len(schema.ARTIFACT_SCHEMAS) == 9
    for name in EXPECTED_NAMES:
        assert schema.validate_name(name) == []
        spec = schema.ARTIFACT_SCHEMAS[name]
        assert spec.name == name
        assert spec.appendix is not None, f"{name} 必须声明附录 schema"


def test_validate_name_unknown() -> None:
    issues = schema.validate_name("foo.md")
    assert issues and "未登记" in issues[0]


def test_validate_unknown_artifact() -> None:
    artifact = Artifact(name="foo.md", path=Path("foo.md"), meta={"id": "ART-FOO-0001"}, body="")
    assert any("未登记的产物" in item for item in schema.validate(artifact))


@pytest.mark.parametrize("name", sorted(EXPECTED_NAMES))
def test_valid_artifact_passes(tmp_path: Path, name: str) -> None:
    artifact = build(tmp_path, name)
    assert schema.validate(artifact) == []


@pytest.mark.parametrize("name", sorted(EXPECTED_NAMES))
def test_missing_section_detected(tmp_path: Path, name: str) -> None:
    spec = schema.ARTIFACT_SCHEMAS[name]
    dropped = spec.required_sections[0]
    artifact = build(tmp_path, name, sections=spec.required_sections[1:])
    issues = schema.validate(artifact)
    assert any("缺少必备章节" in item and dropped in item for item in issues)


@pytest.mark.parametrize(
    "name,key",
    [
        (name, key)
        for name in sorted(EXPECTED_NAMES)
        for key in schema.ARTIFACT_SCHEMAS[name].required_meta
    ],
)
def test_missing_required_meta_detected(tmp_path: Path, name: str, key: str) -> None:
    artifact = build(tmp_path, name, meta={key: None})
    issues = schema.validate(artifact)
    assert any(f"缺少必备 meta 字段：{key}" in item for item in issues)


@pytest.mark.parametrize("name", sorted(EXPECTED_NAMES))
def test_missing_appendix_detected(tmp_path: Path, name: str) -> None:
    artifact = build(tmp_path, name, appendix=None)
    issues = schema.validate(artifact)
    assert any("缺少结构化附录" in item for item in issues)


@pytest.mark.parametrize("name", sorted(EXPECTED_NAMES))
def test_appendix_required_field_detected(tmp_path: Path, name: str) -> None:
    appendix = copy.deepcopy(valid_appendix(name))
    required = list(schema.ARTIFACT_SCHEMAS[name].appendix["required"])  # type: ignore[index]
    appendix.pop(required[0], None)
    artifact = build(tmp_path, name, appendix=appendix)
    issues = schema.validate(artifact)
    assert any("附录校验失败" in item and required[0] in item for item in issues)


def test_malformed_appendix_json_detected(tmp_path: Path) -> None:
    body = render_body(("仓库画像", "命令基线"), None) + "\n## 附录：结构化数据\n\n```json\n{坏 JSON}\n```\n"
    artifact = build(tmp_path, "repo-profile.md", raw_body=body)
    assert any("缺少结构化附录" in item for item in schema.validate(artifact))


# ---------------------------------------------------------------------- #
# 结构断言（07 §4.3「结构断言」列）
# ---------------------------------------------------------------------- #
def _appendix_with(tmp_path: Path, name: str, mutate) -> Artifact:
    appendix = copy.deepcopy(valid_appendix(name))
    mutate(appendix)
    return build(tmp_path, name, appendix=appendix)


def test_prd_requirement_id_and_acceptance(tmp_path: Path) -> None:
    def mutate(app):
        app["requirements"][0]["id"] = "R-01"
        app["requirements"][0]["acceptance"] = []

    issues = schema.validate(_appendix_with(tmp_path, "prd.md", mutate))
    assert any("需求编号不合规" in item for item in issues)
    assert any("缺少验收标准" in item for item in issues)


def test_prd_requires_at_least_one_requirement(tmp_path: Path) -> None:
    issues = schema.validate(_appendix_with(tmp_path, "prd.md", lambda app: app.update(requirements=[])))
    assert any("附录校验失败" in item and "requirements" in item for item in issues)


def test_exploration_requires_unknowns(tmp_path: Path) -> None:
    issues = schema.validate(_appendix_with(tmp_path, "exploration.md", lambda app: app.update(unknowns=[])))
    assert any("未知项" in item for item in issues)


def test_exploration_unknown_status_enum(tmp_path: Path) -> None:
    def mutate(app):
        app["unknowns"][0]["status"] = "知道"

    issues = schema.validate(_appendix_with(tmp_path, "exploration.md", mutate))
    assert any("附录校验失败" in item for item in issues)


def test_design_assertions(tmp_path: Path) -> None:
    def mutate(app):
        app["options"] = app["options"][:1]
        app["risks"] = app["risks"][:2]
        app["risks"][0]["rollback"] = ""

    issues = schema.validate(_appendix_with(tmp_path, "design.md", mutate))
    assert any("选型备选至少 2" in item for item in issues)
    assert any("风险至少 3" in item for item in issues)
    assert any("缺少回滚手段" in item for item in issues)


def test_repo_profile_assertions(tmp_path: Path) -> None:
    def mutate(app):
        app["commands"] = app["commands"][:2]
        app["entry_points"] = []

    issues = schema.validate(_appendix_with(tmp_path, "repo-profile.md", mutate))
    assert any("附录校验失败" in item and "commands" in item for item in issues)
    assert any("附录校验失败" in item and "entry_points" in item for item in issues)


def test_tasks_assertions(tmp_path: Path) -> None:
    def mutate(app):
        app["tasks"][0].update({"id": "X1", "test_first": False, "estimate_h": 5, "files": []})

    issues = schema.validate(_appendix_with(tmp_path, "tasks.md", mutate))
    assert any("任务编号不合规" in item for item in issues)
    assert any("test_first=true" in item for item in issues)
    assert any("超限" in item for item in issues)
    assert any("附录校验失败" in item and "files" in item for item in issues)


def test_tdd_plan_assertions(tmp_path: Path) -> None:
    def mutate(app):
        app["steps"] = [
            {"n": index + 1, "goal": "步", "files": ["src/a.py"], "verify": ""} for index in range(9)
        ]

    issues = schema.validate(_appendix_with(tmp_path, "tdd-plan.md", mutate))
    assert any("步骤最多 8 步" in item for item in issues)
    assert sum("验证方式" in item for item in issues) == 9


def test_tdd_report_assertions(tmp_path: Path) -> None:
    artifact = build(
        tmp_path,
        "tdd-report.md",
        meta={"verdict": "ok", "rounds": 0, "needs_human": "no"},
    )
    issues = schema.validate(artifact)
    assert any("meta.verdict 非法" in item for item in issues)
    assert any("meta.rounds 应为" in item for item in issues)
    assert any("meta.needs_human 应为布尔值" in item for item in issues)

    bad_task = build(
        tmp_path,
        "tdd-report.md",
        appendix={
            "rounds": 1,
            "verdict": "pass",
            "needs_human": False,
            "tasks": [
                {"task_id": "T1", "rounds": 1, "verdict": "maybe", "passed": 1, "total": 1, "failures": []}
            ],
        },
    )
    assert any("verdict 非法" in item for item in schema.validate(bad_task))


def test_qa_report_assertions(tmp_path: Path) -> None:
    def mutate(app):
        app["evidence"] = app["evidence"][:2]

    issues = schema.validate(_appendix_with(tmp_path, "qa-report.md", mutate))
    assert any("evidence 未覆盖" in item and "rpc" in item for item in issues)

    failed = copy.deepcopy(valid_appendix("qa-report.md"))
    failed["evidence"][0]["status"] = "failed"
    artifact = build(tmp_path, "qa-report.md", appendix=failed, meta={"verdict": "pass"})
    assert any("verdict 必须为 fail" in item for item in schema.validate(artifact))

    bad_meta = build(tmp_path, "qa-report.md", meta={"verdict": "unknown"})
    assert any("meta.verdict 非法" in item for item in schema.validate(bad_meta))


def test_archive_record_assertions(tmp_path: Path) -> None:
    def mutate(app):
        app["rules_proposed"][0]["id"] = "X-1"
        app["rules_proposed"][0]["criteria"] = ""

    issues = schema.validate(_appendix_with(tmp_path, "archive-record.md", mutate))
    assert any("规则编号不合规" in item for item in issues)
    assert any("缺少 criteria" in item for item in issues)