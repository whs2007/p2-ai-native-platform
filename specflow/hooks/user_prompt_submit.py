"""UserPromptSubmit hook：命令解析 → 门控校验 → 摘要注入（docs/07 §5.6、docs/04 §5.3）。

行为：
- 无 `/command` → allow（continue=true）+ 一行阶段摘要（≤120 token 量级）；
- 未知 `/xxx` → allow + 告警（不阻断普通对话）；
- 已知命令 → `engine.gate.check`：
  - 未通过 → block：stdout `continue=false`+`reason`、stderr 单行、exit 2；
  - 同一命令连续失败 ≥ `settings.gate_repeat_limit` → exit 3（NeedsHuman，停止自动重试）；
  - 通过 → 注入上游摘要（`gate.summarize_upstream`，预算 `settings.prompt_budget`）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from specflow.errors import HookError
from specflow.hooks import _support
from specflow.hooks.protocol import HookInput, HookOutcome, block, inject, needs_human
from specflow.hooks.session_start import stage_status_line
from specflow.util import dump_json, estimate_tokens, now_iso, truncate_by_tokens

_COMMAND_RE = re.compile(r"^\s*(/[A-Za-z][A-Za-z0-9_-]*)")


def parse_command(prompt: str | None) -> str | None:
    """只认 prompt 开头（允许前导空白）的 `/slug`。"""
    if not prompt:
        return None
    match = _COMMAND_RE.match(prompt)
    return match.group(1) if match else None


def _gate_modules() -> tuple[Any, Any, Any, Any]:
    """延迟导入 T1 门控依赖；未就绪时抛 HookError 由 manager 降级处理。"""
    try:
        from specflow.artifacts.store import ArtifactStore
        from specflow.engine.gate import check as gate_check
        from specflow.engine.gate import summarize_upstream
        from specflow.engine.state import StateStore
    except ImportError as exc:  # pragma: no cover - 集成期由 Lead 兜底
        raise HookError(f"门控依赖尚未就绪（artifacts/engine）：{exc}") from exc
    return ArtifactStore, gate_check, summarize_upstream, StateStore


# ---------------------------------------------------------------------- #
# 连续失败计数（优先 T1 StateStore；缺失时直接读写 state.json）
# ---------------------------------------------------------------------- #
def _bump_failure(settings: Any, state_store: Any, slug: str) -> int:
    try:
        return int(state_store.record_gate_failure(slug))
    except Exception:
        data = _support.load_state_dict(settings)
        failures = data.get("gate_failures")
        failures = dict(failures) if isinstance(failures, dict) else {}
        count = int(failures.get(slug) or 0) + 1
        failures[slug] = count
        data["gate_failures"] = failures
        data["updated_at"] = now_iso()
        try:
            dump_json(Path(settings.state_path), data)
        except OSError:  # pragma: no cover - 只读环境
            pass
        return count


def _clear_failures(settings: Any, state_store: Any, slug: str) -> None:
    try:
        state_store.clear_gate_failures(slug)
    except Exception:
        data = _support.load_state_dict(settings)
        failures = data.get("gate_failures")
        if isinstance(failures, dict) and slug in failures:
            failures.pop(slug, None)
            data["gate_failures"] = failures
            try:
                dump_json(Path(settings.state_path), data)
            except OSError:  # pragma: no cover
                pass


def _remember_last_command(state_store: Any, prompt: str | None) -> None:
    line = (prompt or "").strip().splitlines()
    if not line:
        return
    try:
        state = state_store.state
        state.last_command = line[0][:200]
        state_store.save(state)
    except Exception:  # 状态写失败不影响放行
        pass


# ---------------------------------------------------------------------- #
# 文案
# ---------------------------------------------------------------------- #
def _missing_text(result: Any) -> str:
    missing = list(getattr(result, "missing", []) or [])
    if missing:
        return "\n".join(f"- {item}" for item in missing)
    reasons = list(getattr(result, "reasons", []) or [])
    if reasons:
        return "\n".join(f"- {item}" for item in reasons)
    return "- 上游产物未就绪或状态不符。"


def _stage_summary(state: dict[str, Any], settings: Any) -> tuple[str, dict[str, Any]]:
    line, info = stage_status_line(state, settings)
    extras = ["未识别阶段命令，按普通对话放行"]
    if info.get("needs_human"):
        extras.append("needs_human=true，需人工介入")
    extras.append(f"输入 {info['stage']} 对应的 /命令 可自动门控")
    return f"{line}｜{'；'.join(extras)}", info


def _upstream_artifacts(defn: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for raw in defn.get("requires") or ():
        name = str(raw).split(":", 1)[0]
        if name and name not in refs:
            refs.append(name)
    return refs


# ---------------------------------------------------------------------- #
# handler
# ---------------------------------------------------------------------- #
def handle(payload: HookInput, settings: Any) -> HookOutcome:
    prompt = payload.prompt or ""
    command = parse_command(prompt)
    state = _support.load_state_dict(settings)

    # 1) 无命令：放行 + 一行阶段摘要
    if command is None:
        context, info = _stage_summary(state, settings)
        context, _ = truncate_by_tokens(context, max(1, int(settings.prompt_budget or 300)))
        meta = {
            "stage": info.get("stage"),
            "context_tokens": estimate_tokens(context),
        }
        return inject("UserPromptSubmit", context, meta=meta)

    # 2) 解析阶段；未知命令放行 + 告警
    stage_obj, defn, degraded = _support.resolve_stage(settings, command)
    if defn is None:
        known = "、".join(sorted(_support.load_stage_defs(settings))) or "（阶段表不可用）"
        context = (
            f"未知命令 {command}：未匹配任何阶段命令，本次按普通对话放行。"
            f"可用命令：{known}"
        )
        return inject(
            "UserPromptSubmit", context,
            meta={"unknown_command": command, "degraded": degraded},
        )

    stage_id = str(defn.get("id") or state.get("stage") or "")
    slug = str(defn.get("slug") or command.lstrip("/"))
    if stage_obj is None:  # engine.stages 与内置表都不可用：降级放行
        context = (
            f"阶段表不可用（engine.stages/skills 未就绪），已跳过 {command} 的门控检查，"
            "本次按普通对话放行；请先执行 /project-init 或检查 skills/。"
        )
        return inject(
            "UserPromptSubmit", context,
            meta={"command": command, "stage": stage_id, "gate": "skip", "degraded": True},
        )

    ArtifactStore, gate_check, summarize_upstream, StateStore = _gate_modules()
    store = ArtifactStore(settings)
    state_store = StateStore(Path(settings.state_path))

    # 3) 门控
    try:
        result = gate_check(stage_obj, store, state_store.load())
    except Exception as exc:  # 门控实现异常 → fail-open（由 manager 记 trace error）
        raise HookError(f"门控执行失败（{command}）：{exc}") from exc

    if not getattr(result, "passed", False):
        failures = _bump_failure(settings, state_store, slug)
        limit = max(1, int(getattr(settings, "gate_repeat_limit", 3) or 3))
        detail = _missing_text(result)
        howto = getattr(result, "howto", None)
        meta: dict[str, Any] = {
            "stage": stage_id,
            "gate": "fail",
            "command": command,
            "failures": failures,
            "missing": list(getattr(result, "missing", []) or []),
        }
        if failures >= limit:
            meta["needs_human"] = True
            return needs_human(
                f"门控连续 {failures} 次未通过（{command}），已停止自动重试，请人工介入停止调用模型。\n"
                f"{detail}",
                howto=howto,
                meta=meta,
            )
        return block(
            f"门控未通过：{command}\n{detail}\n未调用模型。",
            howto=howto,
            meta=meta,
        )

    # 4) 通过：清计数、注入上游摘要
    _clear_failures(settings, state_store, slug)
    _remember_last_command(state_store, prompt)

    budget = max(1, int(getattr(settings, "prompt_budget", 300) or 300))
    try:
        upstream = summarize_upstream(stage_obj, store, budget)
    except Exception as exc:
        upstream = f"（上游摘要生成失败：{exc}）"
    title = str(defn.get("title") or "").strip()
    status_line = f"阶段 {stage_id} {title}｜门控通过".strip()
    context = f"{status_line}\n{upstream}".strip()
    context, truncated = truncate_by_tokens(context, budget)
    tokens = estimate_tokens(context)
    meta = {
        "stage": stage_id,
        "gate": "pass",
        "command": command,
        "context_tokens": tokens,
        "artifact_refs": _upstream_artifacts(defn),
        "degraded": degraded,
        "truncated": truncated,
    }
    return inject("UserPromptSubmit", context, meta=meta)