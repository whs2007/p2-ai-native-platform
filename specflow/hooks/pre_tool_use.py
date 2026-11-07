"""PreToolUse hook：W/D/A/C 规则匹配 → allow/ask/deny（docs/07 §5.5/§5.6、docs/04 §5.4）。

退出码：本事件**始终 exit 0**，决策走 stdout 的 `hookSpecificOutput.permissionDecision`；
deny 必须带替代方案。C 类需要上下文（stage / role / tasks.md 当前任务），
上下文来源优先级：事件载荷字段 > 环境变量 > `state.last_command --task` 推断。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from specflow.hooks import _support
from specflow.hooks.protocol import HookInput, HookOutcome
from specflow.hooks.rules import RuleHit, check_tool_use

_SEVERITY_MAP = {"deny": "block", "confirm": "confirm", "allow": "allow"}
_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)
_TASK_FLAG_RE = re.compile(r"--task[=\s]+(\S+)")


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _read_appendix_fallback(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, PermissionError, UnicodeDecodeError):
        return None
    for block in reversed(_JSON_BLOCK_RE.findall(text)):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _load_tasks(settings: Any) -> list[dict[str, Any]]:
    """读 tasks.md 结构化附录里的任务列表（优先 T1 ArtifactStore，缺失时直接解析 JSON 块）。"""
    appendix: Any = None
    try:
        from specflow.artifacts.store import ArtifactStore  # 延迟导入

        artifact = ArtifactStore(settings).load_or_none("tasks.md")
        appendix = artifact.appendix() if artifact is not None else None
    except Exception:
        appendix = _read_appendix_fallback(Path(settings.artifacts_dir) / "tasks.md")
    if not isinstance(appendix, Mapping):
        return []
    tasks = appendix.get("tasks")
    if not isinstance(tasks, list):
        return []
    return [item for item in tasks if isinstance(item, dict)]


def _task_id_from_state(state: Mapping[str, Any]) -> str:
    command = str(state.get("last_command") or "")
    match = _TASK_FLAG_RE.search(command)
    return match.group(1) if match else ""


def build_context(payload: HookInput, settings: Any) -> dict[str, Any]:
    """组装 C 类规则需要的上下文；无上下文时 C 类自动跳过（07 §5.5 末尾）。"""
    project_dir = Path(payload.cwd or settings.project_dir)
    context: dict[str, Any] = {
        "project_dir": project_dir,
        "artifacts_dir": Path(settings.artifacts_dir),
    }
    state = _support.load_state_dict(settings)
    context["state"] = state
    context["approved_artifacts"] = sorted(_support.approved_artifacts(state))
    stage_id = str(state.get("stage") or "")
    if stage_id:
        context["stage"] = stage_id

    role = _first_text(
        payload.raw.get("agent_role"),
        payload.raw.get("role"),
        os.environ.get("SPECFLOW_ROLE"),
    )
    if role:
        context["role"] = role.lower()

    task_id = _first_text(
        payload.raw.get("task_id"),
        os.environ.get("SPECFLOW_TASK_ID"),
        _task_id_from_state(state),
    )
    if task_id:
        context["task_id"] = task_id
        for task in _load_tasks(settings):
            if str(task.get("id") or "") == task_id:
                context["task"] = task
                break
    return context


def _decision_payload(hit: RuleHit, context: Mapping[str, Any]) -> dict[str, Any]:
    decision = hit.decision
    specific: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
    }
    if decision != "allow":
        reason = hit.reason
        if hit.alternative:
            reason = f"{reason}替代方案：{hit.alternative}"
        specific["permissionDecisionReason"] = reason
    meta: dict[str, Any] = {
        "rule_id": hit.rule_id or None,
        "severity": _SEVERITY_MAP.get(hit.severity, hit.severity),
    }
    if context.get("stage"):
        meta["stage"] = context["stage"]
    task = context.get("task")
    if isinstance(task, Mapping) and task.get("id"):
        meta["task_id"] = str(task["id"])
    if hit.alternative:
        meta["alternative"] = hit.alternative
    return {"hookSpecificOutput": specific, "specflow": meta}


def handle(payload: HookInput, settings: Any) -> HookOutcome:
    """PreToolUse：规则判定 → allow/ask/deny JSON，始终 exit 0。"""
    tool_name = payload.tool_name or _first_text(payload.raw.get("tool_name"))
    tool_input = payload.tool_input or {}
    context = build_context(payload, settings)
    hit = check_tool_use(tool_name, tool_input, context)
    result = _decision_payload(hit, context)
    return HookOutcome(
        payload=result,
        exit_code=0,
        decision=hit.decision,
        rule_id=hit.rule_id or None,
        context_tokens=0,
    )