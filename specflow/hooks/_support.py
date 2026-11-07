"""hooks 内部共享支撑：state 读取、阶段解析（含 07 内置降级表）。

为什么需要降级表：阶段元数据的唯一事实源是 `skills/<slug>/SKILL.md`（ADR-002），
由 T1 的 `engine.stages.load_stages` 解析。hook 是独立进程、必须冷启动可控，
在 skills/ 或 T1 模块尚未就绪时按 07 §5.1/§6 的内置表降级（并在 specflow meta
里标记 `degraded`），保证 SessionStart / UserPromptSubmit 不因上层模块缺失而 fail-open
掉门控能力。内置表与 SKILL.md 冲突时以 SKILL.md 为准。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from specflow.util import load_json

#: 07 §5.1 / §6 的阶段基线（requirements 语法：`artifact.md` 或 `artifact.md:approved`）
FALLBACK_STAGE_DEFS: tuple[dict[str, Any], ...] = (
    {
        "id": "S0A", "command": "/project-init", "slug": "project-init",
        "title": "项目初始化", "requires": (), "produces": ("repo-profile.md",),
        "human_gate": None, "next": "S0B", "max_context_tokens": 3000,
    },
    {
        "id": "S0B", "command": "/domain-input", "slug": "domain-input",
        "title": "业务域输入", "requires": (), "produces": ("domain.md", "index.md"),
        "human_gate": None, "next": "S1", "max_context_tokens": 3000,
    },
    {
        "id": "S1", "command": "/prd-input", "slug": "prd-input",
        "title": "PRD 输入", "requires": (), "produces": ("prd.md",),
        "human_gate": None, "next": "S2", "max_context_tokens": 3000,
    },
    {
        "id": "S2", "command": "/explore", "slug": "explore",
        "title": "综合探索", "requires": ("prd.md",), "produces": ("exploration.md",),
        "human_gate": None, "next": "S3", "max_context_tokens": 3000,
    },
    {
        "id": "S3", "command": "/tech-design", "slug": "tech-design",
        "title": "技术方案", "requires": ("exploration.md",), "produces": ("design.md",),
        "human_gate": None, "next": "S4", "max_context_tokens": 3000,
    },
    {
        "id": "S4", "command": "/plan-task", "slug": "plan-task",
        "title": "任务拆解", "requires": ("design.md:approved",), "produces": ("tasks.md",),
        "human_gate": "design", "next": "S5", "max_context_tokens": 3000,
    },
    {
        "id": "S5", "command": "/add", "slug": "add",
        "title": "TDD 实现", "requires": ("tasks.md",),
        "produces": ("tdd-plan.md", "tdd-report.md"),
        "human_gate": None, "next": "S6", "max_context_tokens": 3000,
    },
    {
        "id": "S6", "command": "/qa-validate", "slug": "qa-validate",
        "title": "QA 验证", "requires": ("tdd-report.md",), "produces": ("qa-report.md",),
        "human_gate": None, "next": "S7", "max_context_tokens": 3000,
    },
    {
        "id": "S7", "command": "/archive", "slug": "archive",
        "title": "归档回灌", "requires": ("qa-report.md",), "produces": ("archive-record.md",),
        "human_gate": None, "next": None, "max_context_tokens": 3000,
    },
)

_FALLBACK_BY_COMMAND = {item["command"]: item for item in FALLBACK_STAGE_DEFS}
_FALLBACK_BY_SLUG = {item["slug"]: item for item in FALLBACK_STAGE_DEFS}
_FALLBACK_BY_ID = {item["id"]: item for item in FALLBACK_STAGE_DEFS}


# ---------------------------------------------------------------------- #
# state.json
# ---------------------------------------------------------------------- #
def load_state_dict(settings: Any) -> dict[str, Any]:
    """读 `.spec/state.json`；优先 T1 StateStore，缺失时直接读 JSON 降级。"""
    state_path = Path(settings.state_path)
    try:
        from specflow.engine.state import StateStore  # 延迟导入：避免 import 期硬依赖

        return StateStore(state_path).load().to_dict()
    except Exception:  # ImportError / 解析失败都降级为只读 JSON
        data = load_json(state_path, {})
        return data if isinstance(data, dict) else {}


def approved_artifacts(state: Mapping[str, Any] | None) -> set[str]:
    """从 state 里取 status=approved 的产物名。"""
    result: set[str] = set()
    if not isinstance(state, Mapping):
        return result
    artifacts = state.get("artifacts")
    if isinstance(artifacts, Mapping):
        for name, info in artifacts.items():
            if isinstance(info, Mapping) and info.get("status") == "approved":
                result.add(str(name))
    return result


def gate_status(state: Mapping[str, Any] | None, name: str) -> str:
    if not isinstance(state, Mapping):
        return "pending"
    gates = state.get("gates")
    if isinstance(gates, Mapping):
        info = gates.get(name)
        if isinstance(info, Mapping):
            return str(info.get("status") or "pending")
    return "pending"


# ---------------------------------------------------------------------- #
# 阶段解析
# ---------------------------------------------------------------------- #
def _norm_command(command: str) -> str:
    text = (command or "").strip()
    if not text:
        return ""
    return text if text.startswith("/") else f"/{text}"


def _def_from_stage(stage: Any) -> dict[str, Any]:
    requires = []
    for item in getattr(stage, "requires", ()) or ():
        artifact = getattr(item, "artifact", None)
        status = getattr(item, "status", None)
        if artifact is None:
            requires.append(str(item))
        elif status:
            requires.append(f"{artifact}:{status}")
        else:
            requires.append(str(artifact))
    produces = tuple(str(item) for item in (getattr(stage, "produces", ()) or ()))
    return {
        "id": str(getattr(stage, "id", "") or ""),
        "command": _norm_command(str(getattr(stage, "command", "") or "")),
        "slug": str(getattr(stage, "slug", "") or ""),
        "title": str(getattr(stage, "title", "") or ""),
        "requires": tuple(requires),
        "produces": produces,
        "human_gate": getattr(stage, "human_gate", None) or None,
        "next": getattr(stage, "next", None) or None,
        "max_context_tokens": int(getattr(stage, "max_context_tokens", 3000) or 3000),
    }


def _with_fallback(command: str) -> dict[str, Any] | None:
    normalized = _norm_command(command)
    defn = _FALLBACK_BY_COMMAND.get(normalized)
    if defn is None and normalized:
        defn = _FALLBACK_BY_SLUG.get(normalized.lstrip("/"))
    return dict(defn) if defn else None


def load_stage_defs(settings: Any) -> dict[str, dict[str, Any]]:
    """返回 `{command: {"obj": Stage|None, "def": dict}}`（优先 SKILL.md，降级内置表）。"""
    defs: dict[str, dict[str, Any]] = {}
    try:
        from specflow.engine.stages import load_stages  # 延迟导入：T1 可能尚未就绪

        stages = load_stages(Path(settings.skills_dir))
    except Exception:
        stages = None
    if stages:
        for command, stage in stages.items():
            defn = _def_from_stage(stage)
            key = defn["command"] or _norm_command(str(command))
            defs[key] = {"obj": stage, "def": defn}
    if defs:
        return defs
    return {
        item["command"]: {"obj": None, "def": dict(item)}
        for item in FALLBACK_STAGE_DEFS
    }


def _build_stage_object(defn: Mapping[str, Any]) -> Any | None:
    """用 engine.stages.Stage 数据类造一个降级 Stage（供 gate.check 使用）。"""
    try:
        from specflow.engine.stages import Requirement, Stage
    except Exception:
        return None
    requirements = []
    for raw in defn.get("requires") or ():
        text = str(raw)
        if ":" in text:
            artifact, status = text.split(":", 1)
            requirements.append(Requirement(artifact=artifact, status=status))
        else:
            requirements.append(Requirement(artifact=text))
    command = str(defn.get("command") or "")
    return Stage(
        id=str(defn.get("id") or ""),
        command=command,
        slug=str(defn.get("slug") or command.lstrip("/")),
        title=str(defn.get("title") or ""),
        requires=tuple(requirements),
        produces=tuple(str(item) for item in (defn.get("produces") or ())),
        human_gate=defn.get("human_gate") or None,
        max_context_tokens=int(defn.get("max_context_tokens") or 3000),
        skill_path=Path(defn.get("skill_path") or ""),
        next=defn.get("next") or None,
    )


def resolve_stage(settings: Any, command: str) -> tuple[Any | None, dict[str, Any] | None, bool]:
    """按命令找阶段：返回 (Stage 对象, 定义 dict, 是否降级)。

    优先 `engine.stages.load_stages` 的真实 Stage；不可用时用内置表造 Stage。
    都拿不到 Stage 对象时返回 (None, defn, True)，由调用方 fail-open。
    """
    defs = load_stage_defs(settings)
    normalized = _norm_command(command)
    entry = defs.get(normalized)
    degraded = False
    if entry is None:
        entry = defs.get(f"/{normalized.lstrip('/')}")
    if entry is None:
        defn = _with_fallback(normalized)
        if defn is None:
            return None, None, False
        entry = {"obj": None, "def": defn}
        degraded = True
    stage_obj = entry.get("obj")
    defn = entry.get("def") or {}
    if stage_obj is None:
        stage_obj = _build_stage_object(defn)
        degraded = True
    if stage_obj is None:
        return None, defn, True
    return stage_obj, defn, degraded


def resolve_stage_by_id(settings: Any, stage_id: str) -> tuple[Any | None, dict[str, Any] | None, bool]:
    """按 S0A..S7 找阶段（SessionStart 用）。"""
    defs = load_stage_defs(settings)
    for entry in defs.values():
        if str((entry.get("def") or {}).get("id") or "") == str(stage_id):
            defn = entry.get("def") or {}
            stage_obj = entry.get("obj") or _build_stage_object(defn)
            degraded = entry.get("obj") is None
            return stage_obj, defn, degraded
    defn = _FALLBACK_BY_ID.get(str(stage_id))
    if defn is None:
        return None, None, False
    defn = dict(defn)
    return _build_stage_object(defn), defn, True


def stage_defs_by_id(settings: Any) -> dict[str, dict[str, Any]]:
    """`{stage_id: defn}`，用于展示「下一步命令」。"""
    result: dict[str, dict[str, Any]] = {}
    for entry in load_stage_defs(settings).values():
        defn = entry.get("def") or {}
        stage_id = str(defn.get("id") or "")
        if stage_id:
            result[stage_id] = defn
    return result


def next_command(defs_by_id: Mapping[str, Mapping[str, Any]], stage_id: str | None) -> str | None:
    if not stage_id:
        return None
    defn = defs_by_id.get(str(stage_id))
    if not defn:
        return None
    following = defn.get("next")
    if not following:
        return None
    target = defs_by_id.get(str(following))
    if target and target.get("command"):
        return str(target["command"])
    return str(following)