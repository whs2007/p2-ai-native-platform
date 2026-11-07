"""Hooks 事件分发 + 计时 + trace + 异常降级（docs/07 §3.3/§5.6/§7）。

`run_hook` 是 CLI `specflow hook <Event>` 的真正入口：
读 stdin → dispatch → 计时 → trace 追加 `.spec/traces/hooks-YYYYMMDD.jsonl` → write_outcome。

异常策略（07 §7）：默认 **fail-open**（allow/continue + trace 记 error）；
`SPECFLOW_STRICT_HOOKS=1`（`settings.strict_hooks`）时 **fail-closed**
（PreToolUse 给 deny JSON，其余事件 exit 2）。
"""
from __future__ import annotations

import importlib
import os
import time
from pathlib import Path
from typing import Any

from specflow.errors import HookError
from specflow.hooks.protocol import (
    EXIT_BLOCK,
    EXIT_OK,
    HookInput,
    HookOutcome,
    block,
    inject,
    write_outcome,
)
from specflow.util import append_jsonl, now_iso, today_stamp

#: 事件 → handler 模块（延迟 import，避免 hook 冷启动加载整棵依赖树）
_EVENT_MODULES: dict[str, str] = {
    "SessionStart": "specflow.hooks.session_start",
    "UserPromptSubmit": "specflow.hooks.user_prompt_submit",
    "PreToolUse": "specflow.hooks.pre_tool_use",
    "PostToolUse": "specflow.hooks.post_tool_use",
}

_CANONICAL_EVENTS = {
    name.lower().replace("-", "").replace("_", ""): name for name in _EVENT_MODULES
}

_FAIL_OPEN_HINT = "已 fail-open 放行；如要严格阻断请设置 SPECFLOW_STRICT_HOOKS=1。"


# ---------------------------------------------------------------------- #
# 事件解析
# ---------------------------------------------------------------------- #
def canonical_event(event: str) -> str | None:
    key = (event or "").strip().lower().replace("-", "").replace("_", "")
    return _CANONICAL_EVENTS.get(key)


def _load_handler(event: str) -> Any:
    canonical = canonical_event(event)
    if canonical is None:
        raise HookError(f"未知 hook 事件：{event!r}（支持 {', '.join(_EVENT_MODULES)}）")
    try:
        return importlib.import_module(_EVENT_MODULES[canonical])
    except ImportError as exc:  # pragma: no cover - 集成期由 Lead 兜底
        raise HookError(f"hook handler 导入失败（{canonical}）：{exc}") from exc


def dispatch(event: str, payload: HookInput, settings: Any) -> HookOutcome:
    """按事件名调用对应 handler.handle(payload, settings)。"""
    module = _load_handler(event)
    handler = getattr(module, "handle", None)
    if not callable(handler):
        raise HookError(f"{module.__name__} 缺少 handle(payload, settings)")
    outcome = handler(payload, settings)
    if not isinstance(outcome, HookOutcome):
        raise HookError(f"{module.__name__}.handle 返回值不是 HookOutcome")
    return outcome


# ---------------------------------------------------------------------- #
# 异常降级
# ---------------------------------------------------------------------- #
def _strict_enabled(settings: Any) -> bool:
    if settings is not None:
        return bool(getattr(settings, "strict_hooks", False))
    raw = str(os.environ.get("SPECFLOW_STRICT_HOOKS", "")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _fallback_outcome(event: str, settings: Any, error: str) -> HookOutcome:
    """hook 异常时的降级决策：默认 fail-open，strict 时 fail-closed。"""
    canonical = canonical_event(event)
    label = canonical or (event or "Unknown")
    if _strict_enabled(settings):
        message = f"specflow hook 异常（strict 模式 fail-closed）：{error}"
        if canonical == "PreToolUse":
            payload = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": message,
                },
                "specflow": {"error": error, "strict": True, "degraded": True},
            }
            return HookOutcome(
                payload=payload,
                exit_code=EXIT_OK,
                decision="deny",
                error=error,
                stderr=message,
            )
        return block(message, meta={"error": error, "strict": True, "degraded": True})
    if canonical == "PreToolUse":
        message = f"specflow hook 异常，已 fail-open（allow）：{error}"
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": message,
            },
            "specflow": {"error": error, "degraded": True},
        }
        return HookOutcome(payload=payload, exit_code=EXIT_OK, decision="allow", error=error)
    context = f"specflow hook 异常，{_FAIL_OPEN_HINT}原因：{error}"
    outcome = inject(label, context, meta={"error": error, "degraded": True})
    outcome.error = error
    return outcome


# ---------------------------------------------------------------------- #
# trace
# ---------------------------------------------------------------------- #
def trace_path(settings: Any) -> Path:
    return Path(settings.traces_dir) / f"hooks-{today_stamp()}.jsonl"


def _record_trace(
    settings: Any,
    event: str,
    session_id: str,
    outcome: HookOutcome,
    elapsed_ms: int,
) -> None:
    if settings is None:
        return
    record = {
        "ts": now_iso(),
        "session_id": session_id or "",
        "event": event,
        "decision": outcome.decision,
        "rule_id": outcome.rule_id or None,
        "exit_code": int(outcome.exit_code),
        "elapsed_ms": int(elapsed_ms),
        "context_tokens": int(outcome.context_tokens),
        "artifact_refs": list(outcome.artifact_refs),
        "error": outcome.error,
    }
    try:
        append_jsonl(trace_path(settings), record)
    except OSError:  # trace 失败不能影响 hook 决策
        pass


def _stamp_elapsed(outcome: HookOutcome, elapsed_ms: int) -> None:
    outcome.elapsed_ms = elapsed_ms
    payload = outcome.payload
    if not isinstance(payload, dict):
        return
    meta = payload.get("specflow")
    if not isinstance(meta, dict):
        meta = {}
        payload["specflow"] = meta
    meta.setdefault("elapsed_ms", elapsed_ms)


# ---------------------------------------------------------------------- #
# 入口
# ---------------------------------------------------------------------- #
def _load_settings() -> Any:
    from specflow.config import Settings

    return Settings.load()


def run_hook(event: str, settings: Any = None) -> int:
    """读 stdin → dispatch → trace → 写 stdout，返回退出码。"""
    started = time.perf_counter()
    session_id = ""
    resolved_settings = settings
    error: str | None = None

    try:
        if resolved_settings is None:
            resolved_settings = _load_settings()
    except Exception as exc:  # 配置加载失败也算 hook 异常
        resolved_settings = None
        error = f"{type(exc).__name__}: {exc}"
        outcome = _fallback_outcome(event, None, error)
    else:
        try:
            hook_input = HookInput.from_stdin()
            session_id = hook_input.session_id
            outcome = dispatch(event, hook_input, resolved_settings)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            outcome = _fallback_outcome(event, resolved_settings, error)

    if outcome.error is None and error is not None:
        outcome.error = error
    elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
    _stamp_elapsed(outcome, elapsed_ms)
    _record_trace(resolved_settings, canonical_event(event) or event, session_id, outcome, elapsed_ms)
    return write_outcome(outcome)