"""各产物的结构契约：必备 meta / 必备章节 / 附录 JSON Schema / 跨字段断言。

来源：docs/07 §4.3（与 docs/04 §4 对齐，附录字段为强校验来源）。
`validate(artifact) -> list[str]`，空列表表示通过；`status --check`、门控、`gate approve` 共用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from specflow import jsonschema
from specflow.artifacts.store import Artifact

#: 所有产物共用的必备 meta（由 ArtifactStore 自动维护）
COMMON_REQUIRED_META: tuple[str, ...] = ("id", "name", "stage", "version", "status", "hash")

_REQ_ID = re.compile(r"^REQ-\d{2,}$")
_TASK_ID = re.compile(r"^T\d+$")
_RULE_ID = re.compile(r"^R-\d{2,}$")

_STR = {"type": "string"}
_STR1 = {"type": "string", "minLength": 1}
_STR_LIST = {"type": "array", "items": {"type": "string"}}


# ---------------------------------------------------------------------- #
# 各产物附录 JSON Schema
# ---------------------------------------------------------------------- #
_REPO_PROFILE_APPENDIX: dict[str, Any] = {
    "type": "object",
    "required": [
        "language",
        "package_manager",
        "test_command",
        "entry_points",
        "convention_files",
        "commands",
    ],
    "properties": {
        "language": _STR1,
        "package_manager": _STR1,
        "test_command": _STR1,
        "entry_points": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "convention_files": _STR_LIST,
        "commands": {
            "type": "array",
            "minItems": 3,
            "items": {
                "type": "object",
                "required": ["purpose", "command"],
                "properties": {"purpose": _STR1, "command": _STR1},
            },
        },
    },
}

_PRD_APPENDIX: dict[str, Any] = {
    "type": "object",
    "required": ["requirements"],
    "properties": {
        "title": _STR,
        "requirements": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "title", "desc", "acceptance"],
                "properties": {
                    "id": _STR1,
                    "title": _STR1,
                    "desc": _STR,
                    "acceptance": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                },
            },
        },
    },
}

_EXPLORATION_APPENDIX: dict[str, Any] = {
    "type": "object",
    "required": ["modules", "constraints", "unknowns"],
    "properties": {
        "modules": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["path", "purpose"],
                "properties": {"path": _STR1, "purpose": _STR},
            },
        },
        "constraints": _STR_LIST,
        "unknowns": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["item", "status"],
                "properties": {
                    "item": _STR1,
                    "status": {"type": "string", "enum": ["待确认", "已确认", "无"]},
                },
            },
        },
        "deps": _STR_LIST,
    },
}

_DESIGN_APPENDIX: dict[str, Any] = {
    "type": "object",
    "required": ["options", "changes", "risks"],
    "properties": {
        "gate": _STR,
        "options": {
            "type": "array",
            "minItems": 2,
            "items": {
                "type": "object",
                "required": ["name", "pros", "cons"],
                "properties": {"name": _STR1, "pros": _STR, "cons": _STR},
            },
        },
        "changes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["path", "change"],
                "properties": {"path": _STR1, "change": _STR},
            },
        },
        "interfaces": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "detail"],
                "properties": {"name": _STR1, "detail": _STR},
            },
        },
        "risks": {
            "type": "array",
            "minItems": 3,
            "items": {
                "type": "object",
                "required": ["id", "desc", "mitigation", "rollback"],
                "properties": {
                    "id": _STR1,
                    "desc": _STR1,
                    "mitigation": _STR,
                    "rollback": _STR1,
                },
            },
        },
    },
}

_TASKS_APPENDIX: dict[str, Any] = {
    "type": "object",
    "required": ["tasks"],
    "properties": {
        "feature_id": _STR,
        "branch": _STR,
        "tasks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": [
                    "id",
                    "title",
                    "files",
                    "test_first",
                    "estimate_h",
                    "acceptance",
                ],
                "properties": {
                    "id": _STR1,
                    "title": _STR1,
                    "desc": _STR,
                    "files": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
                    "test_first": {"const": True},
                    "estimate_h": {"type": "number", "minimum": 0, "maximum": 4},
                    "acceptance": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                },
            },
        },
        "order": _STR_LIST,
    },
}

_TDD_PLAN_APPENDIX: dict[str, Any] = {
    "type": "object",
    "required": ["steps"],
    "properties": {
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "required": ["n", "goal", "files", "verify"],
                "properties": {
                    "n": {"type": "integer", "minimum": 1},
                    "goal": _STR1,
                    "files": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
                    "verify": _STR1,
                },
            },
        }
    },
}

_TDD_REPORT_APPENDIX: dict[str, Any] = {
    "type": "object",
    "required": ["tasks"],
    "properties": {
        "rounds": {"type": "integer", "minimum": 1},
        "verdict": {"type": "string", "enum": ["pass", "fail", "needs_human"]},
        "needs_human": {"type": "boolean"},
        "tasks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["task_id", "rounds", "verdict", "passed", "total", "failures"],
                "properties": {
                    "task_id": _STR1,
                    "rounds": {"type": "integer", "minimum": 1},
                    "verdict": {"type": "string", "enum": ["pass", "fail", "needs_human"]},
                    "passed": {"type": "integer", "minimum": 0},
                    "total": {"type": "integer", "minimum": 0},
                    "failures": {"type": "array"},
                },
            },
        },
    },
}

_QA_REPORT_APPENDIX: dict[str, Any] = {
    "type": "object",
    "required": ["evidence"],
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "evidence": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["type", "status", "detail"],
                "properties": {
                    "type": {"type": "string", "enum": ["unit", "pipeline", "rpc"]},
                    "status": {"type": "string", "enum": ["passed", "failed", "skipped"]},
                    "detail": _STR,
                },
            },
        },
    },
}

_ARCHIVE_RECORD_APPENDIX: dict[str, Any] = {
    "type": "object",
    "required": ["rules_proposed"],
    "properties": {
        "rules_proposed": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "text", "source", "criteria", "counterexample"],
                "properties": {
                    "id": _STR1,
                    "text": _STR1,
                    "source": _STR,
                    "criteria": _STR1,
                    "counterexample": _STR,
                },
            },
        },
        "changes": _STR_LIST,
        "reusable": _STR_LIST,
    },
}


# ---------------------------------------------------------------------- #
# 跨字段结构断言
# ---------------------------------------------------------------------- #
def _appendix_items(artifact: Artifact, key: str) -> list[dict[str, Any]]:
    data = artifact.appendix() or {}
    items = data.get(key)
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _check_prd(artifact: Artifact) -> list[str]:
    issues: list[str] = []
    for item in _appendix_items(artifact, "requirements"):
        requirement_id = item.get("id")
        if isinstance(requirement_id, str) and not _REQ_ID.match(requirement_id):
            issues.append(f"需求编号不合规：{requirement_id!r}（应形如 REQ-01）")
        status = item.get("acceptance")
        if not isinstance(status, list) or not status:
            issues.append(f"需求 {requirement_id or '?'} 缺少验收标准（acceptance ≥1）")
    return issues


def _check_exploration(artifact: Artifact) -> list[str]:
    issues: list[str] = []
    if not _appendix_items(artifact, "unknowns"):
        issues.append("未知项 unknowns 至少 1 条（没有未知项也要写 status=无）")
    return issues


def _check_design(artifact: Artifact) -> list[str]:
    issues: list[str] = []
    alternatives = _appendix_items(artifact, "options")
    if len(alternatives) < 2:
        issues.append(f"选型备选至少 2 个（当前 {len(alternatives)}）")
    changes = _appendix_items(artifact, "changes")
    if not changes:
        issues.append("模块改动 changes 至少 1 条")
    for index, item in enumerate(changes):
        if not str(item.get("path") or "").strip():
            issues.append(f"模块改动第 {index + 1} 条缺少文件路径 path")
    risks = _appendix_items(artifact, "risks")
    if len(risks) < 3:
        issues.append(f"风险至少 3 条（当前 {len(risks)}）")
    for index, item in enumerate(risks):
        if not str(item.get("rollback") or "").strip():
            issues.append(f"风险第 {index + 1} 条缺少回滚手段 rollback")
    return issues


def _check_tasks(artifact: Artifact) -> list[str]:
    issues: list[str] = []
    tasks = _appendix_items(artifact, "tasks")
    if not tasks:
        issues.append("任务表 tasks 至少 1 条")
    for index, item in enumerate(tasks):
        task_id = item.get("id")
        if not isinstance(task_id, str) or not _TASK_ID.match(task_id):
            issues.append(f"任务编号不合规：{task_id!r}（应形如 T1）")
        if item.get("test_first") is not True:
            issues.append(f"任务 {task_id or index + 1} 必须 test_first=true")
        estimate = item.get("estimate_h")
        if isinstance(estimate, (int, float)) and not isinstance(estimate, bool):
            if not 0 < float(estimate) <= 4:
                issues.append(f"任务 {task_id or index + 1} 预估 {estimate}h 超限（0 < h ≤ 4）")
    return issues


def _check_tdd_plan(artifact: Artifact) -> list[str]:
    issues: list[str] = []
    steps = _appendix_items(artifact, "steps")
    if len(steps) > 8:
        issues.append(f"步骤最多 8 步（当前 {len(steps)}）")
    for index, item in enumerate(steps):
        if not str(item.get("verify") or "").strip():
            issues.append(f"第 {index + 1} 步缺少验证方式 verify")
        files = item.get("files")
        if not isinstance(files, list) or not files:
            issues.append(f"第 {index + 1} 步缺少目标文件 files")
    return issues


def _check_tdd_report(artifact: Artifact) -> list[str]:
    issues: list[str] = []
    verdict = artifact.meta.get("verdict")
    if verdict not in {"pass", "fail", "needs_human"}:
        issues.append(f"meta.verdict 非法：{verdict!r}（可选 pass/fail/needs_human）")
    rounds = artifact.meta.get("rounds")
    if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 1:
        issues.append(f"meta.rounds 应为 ≥1 的整数（当前 {rounds!r}）")
    if not isinstance(artifact.meta.get("needs_human"), bool):
        issues.append(f"meta.needs_human 应为布尔值（当前 {artifact.meta.get('needs_human')!r}）")
    for item in _appendix_items(artifact, "tasks"):
        if "verdict" in item and item["verdict"] not in {"pass", "fail", "needs_human"}:
            issues.append(f"任务 {item.get('task_id')!r} 的 verdict 非法：{item['verdict']!r}")
    return issues


def _check_qa_report(artifact: Artifact) -> list[str]:
    issues: list[str] = []
    verdict = artifact.meta.get("verdict")
    if verdict not in {"pass", "fail"}:
        issues.append(f"meta.verdict 非法：{verdict!r}（可选 pass/fail）")
    evidence = _appendix_items(artifact, "evidence")
    covered = {item.get("type") for item in evidence}
    missing = {"unit", "pipeline", "rpc"} - covered
    if missing:
        issues.append(f"evidence 未覆盖：{'、'.join(sorted(missing))}")
    failed = [item for item in evidence if item.get("status") == "failed"]
    if failed and verdict != "fail":
        issues.append("存在 failed 证据时 verdict 必须为 fail")
    return issues


def _check_archive_record(artifact: Artifact) -> list[str]:
    issues: list[str] = []
    for item in _appendix_items(artifact, "rules_proposed"):
        rule_id = item.get("id")
        if not isinstance(rule_id, str) or not _RULE_ID.match(rule_id):
            issues.append(f"规则编号不合规：{rule_id!r}（应形如 R-07）")
        for key in ("text", "criteria"):
            if not str(item.get(key) or "").strip():
                issues.append(f"规则 {rule_id or '?'} 缺少 {key}")
    return issues


# ---------------------------------------------------------------------- #
# 注册表
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class ArtifactSchema:
    name: str
    required_meta: tuple[str, ...] = ()
    required_sections: tuple[str, ...] = ()
    appendix: Mapping[str, Any] | None = None
    extra_check: Callable[[Artifact], list[str]] | None = None


ARTIFACT_SCHEMAS: dict[str, ArtifactSchema] = {
    "repo-profile.md": ArtifactSchema(
        name="repo-profile.md",
        required_sections=("仓库画像", "命令基线"),
        appendix=_REPO_PROFILE_APPENDIX,
    ),
    "prd.md": ArtifactSchema(
        name="prd.md",
        required_meta=("source",),
        required_sections=("背景", "需求条目", "验收标准", "范围外"),
        appendix=_PRD_APPENDIX,
        extra_check=_check_prd,
    ),
    "exploration.md": ArtifactSchema(
        name="exploration.md",
        required_sections=("受影响模块", "现有约束", "未知项"),
        appendix=_EXPLORATION_APPENDIX,
        extra_check=_check_exploration,
    ),
    "design.md": ArtifactSchema(
        name="design.md",
        required_meta=("gate",),
        required_sections=("选型对比", "模块改动", "接口变更", "风险与回滚"),
        appendix=_DESIGN_APPENDIX,
        extra_check=_check_design,
    ),
    "tasks.md": ArtifactSchema(
        name="tasks.md",
        required_meta=("feature_id",),
        required_sections=("任务表", "依赖顺序"),
        appendix=_TASKS_APPENDIX,
        extra_check=_check_tasks,
    ),
    "tdd-plan.md": ArtifactSchema(
        name="tdd-plan.md",
        required_meta=("task_id", "round"),
        required_sections=("步骤", "验证方式"),
        appendix=_TDD_PLAN_APPENDIX,
        extra_check=_check_tdd_plan,
    ),
    "tdd-report.md": ArtifactSchema(
        name="tdd-report.md",
        required_meta=("task_id", "rounds", "verdict", "needs_human"),
        required_sections=("执行摘要", "失败项", "最终测试结果"),
        appendix=_TDD_REPORT_APPENDIX,
        extra_check=_check_tdd_report,
    ),
    "qa-report.md": ArtifactSchema(
        name="qa-report.md",
        required_meta=("verdict",),
        required_sections=("单测", "pipeline", "rpc", "结论"),
        appendix=_QA_REPORT_APPENDIX,
        extra_check=_check_qa_report,
    ),
    "archive-record.md": ArtifactSchema(
        name="archive-record.md",
        required_sections=("变更摘要", "候选回灌规则", "复用条目"),
        appendix=_ARCHIVE_RECORD_APPENDIX,
        extra_check=_check_archive_record,
    ),
}


# ---------------------------------------------------------------------- #
# 校验入口
# ---------------------------------------------------------------------- #
def validate(artifact: Artifact) -> list[str]:
    """校验单个产物，返回人类可读的问题列表（空 = 通过）。"""
    schema = ARTIFACT_SCHEMAS.get(artifact.name)
    if schema is None:
        return [f"未登记的产物：{artifact.name}（可在 specflow/artifacts/schema.py 登记）"]

    issues: list[str] = []
    meta = artifact.meta or {}

    for key in COMMON_REQUIRED_META:
        if meta.get(key) in (None, ""):
            issues.append(f"缺少必备 meta 字段：{key}")
    if meta.get("name") not in (None, "") and str(meta.get("name")) != artifact.name:
        issues.append(f"meta.name 与文件名不一致：{meta.get('name')} ≠ {artifact.name}")
    if "version" in meta and not isinstance(meta.get("version"), int):
        issues.append(f"meta.version 应为整数（当前 {meta.get('version')!r}）")

    for key in schema.required_meta:
        if meta.get(key) in (None, ""):
            issues.append(f"缺少必备 meta 字段：{key}")

    for section in schema.required_sections:
        if not artifact.has_section(section):
            issues.append(f"缺少必备章节：## {section}")

    if schema.appendix is not None:
        data = artifact.appendix()
        if data is None:
            issues.append("缺少结构化附录（## 附录：结构化数据 下的 ```json 块，或 JSON 非法）")
        else:
            for error in jsonschema.validate(data, schema.appendix):
                issues.append(f"附录校验失败：{error}")

    if schema.extra_check is not None:
        issues.extend(schema.extra_check(artifact))
    return issues


def validate_name(name: str) -> list[str]:
    """文件名是否已登记（空 = 已登记）。"""
    if name in ARTIFACT_SCHEMAS:
        return []
    known = "、".join(ARTIFACT_SCHEMAS)
    return [f"未登记的产物名：{name}（已登记：{known}）"]