"""Hooks 协议层：stdin/stdout JSON、退出码、注入载荷（docs/07 §5.6、docs/04 §5.1）。

退出码约定（ADR-010）：

| 码 | 含义 | 数据通道 |
|---|---|---|
| 0 | 正常（allow/ask/deny 都用 0 + stdout JSON） | stdout JSON |
| 2 | 硬阻断（门控/规则拒绝） | stdout JSON + stderr 单行 ≤200 字 |
| 3 | 需人工介入（连续门控失败等），不自动重试 | stdout JSON 的 specflow 字段 |

本模块只依赖 `specflow.errors` / `specflow.util`，不 import engine/artifacts，
保证 hook 冷启动不被 T1 模块拖慢或拖挂。
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from specflow.errors import HookError
from specflow.util import estimate_tokens

EXIT_OK = 0
EXIT_BLOCK = 2
EXIT_NEEDS_HUMAN = 3

#: stderr 进入模型上下文，必须单行且短（docs/04 §5.1）
STDERR_LIMIT = 200


def _read_stdin_text() -> str:
    """按 UTF-8 读取 stdin 的 JSON（宿主协议统一 UTF-8，含中文 prompt）。

    直接用 ``sys.stdin.buffer`` 解码，绕过 Windows 控制台/管道默认 GBK，
    避免中文 prompt 在宿主与 hook 之间被二次转码。
    """
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        try:
            return buffer.read().decode("utf-8", errors="replace")
        except (OSError, ValueError):  # pragma: no cover - 平台相关
            pass
    return sys.stdin.read()


# ---------------------------------------------------------------------- #
# 输入
# ---------------------------------------------------------------------- #
@dataclass
class HookInput:
    """宿主传给 hook 的一次事件（stdin JSON 解析结果）。"""

    raw: dict[str, Any] = field(default_factory=dict)
    event: str = ""
    session_id: str = ""
    cwd: Path = field(default_factory=Path.cwd)
    prompt: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_response: dict[str, Any] = field(default_factory=dict)
    source: str | None = None

    @classmethod
    def from_stdin(cls) -> "HookInput":
        """读 stdin 的 JSON 事件；空/坏 JSON 抛 HookError（由 manager 降级）。"""
        try:
            text = _read_stdin_text()
        except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - 平台相关
            raise HookError(f"读取 stdin 失败：{exc}") from exc
        if not text.strip():
            raise HookError("stdin 为空：hook 需要宿主传入 JSON 事件")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise HookError(f"stdin 不是合法 JSON：{exc.msg}") from exc
        if not isinstance(raw, dict):
            raise HookError("stdin JSON 必须是对象")
        return cls.from_payload(raw)

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> "HookInput":
        """从已解析的 dict 构造（单元测试/进程内调用用）。"""
        if not isinstance(raw, dict):
            raise HookError("hook 载荷必须是对象")

        def _text(key: str) -> str | None:
            value = raw.get(key)
            return value if isinstance(value, str) and value else None

        cwd_raw = _text("cwd") or os.environ.get("SPECFLOW_PROJECT_DIR") or ""
        cwd = Path(cwd_raw).expanduser() if cwd_raw else Path.cwd()
        try:
            cwd = cwd.resolve()
        except OSError:  # pragma: no cover - 极端路径
            cwd = Path(cwd_raw or os.getcwd())

        session_id = (
            _text("session_id")
            or os.environ.get("SPECFLOW_SESSION_ID")
            or ""
        )

        tool_input = raw.get("tool_input")
        tool_response = raw.get("tool_response")
        return cls(
            raw=raw,
            event=_text("hook_event_name") or "",
            session_id=session_id,
            cwd=cwd,
            prompt=_text("prompt"),
            tool_name=_text("tool_name"),
            tool_input=tool_input if isinstance(tool_input, dict) else {},
            tool_response=tool_response if isinstance(tool_response, dict) else {},
            source=_text("source"),
        )


# ---------------------------------------------------------------------- #
# 输出
# ---------------------------------------------------------------------- #
@dataclass
class HookOutcome:
    """一次 hook 的决策结果；manager 负责补 elapsed_ms 并落 trace。"""

    payload: dict[str, Any] = field(default_factory=dict)
    exit_code: int = EXIT_OK
    stderr: str = ""
    decision: str = "allow"          # allow | block | confirm | needs_human
    rule_id: str | None = None
    context_tokens: int = 0
    artifact_refs: list[str] = field(default_factory=list)
    error: str | None = None
    elapsed_ms: int = 0

    @property
    def specflow_meta(self) -> dict[str, Any]:
        meta = self.payload.get("specflow")
        return meta if isinstance(meta, dict) else {}


def _single_line(text: str) -> str:
    return " ".join(str(text).split())


def _stderr_line(text: str) -> str:
    """stderr 必须单行且 ≤200 字，因为它会进入模型上下文。"""
    return _single_line(text)[:STDERR_LIMIT]


def _compose_reason(reason: str, howto: str | None) -> str:
    reason = str(reason).strip()
    if howto:
        return f"{reason}\n补齐方式：{howto}"
    return reason


def _context_payload(event: str, context: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": context,
        }
    }


def inject(
    event: str,
    context: str,
    *,
    meta: dict[str, Any] | None = None,
    rule_id: str | None = None,
) -> HookOutcome:
    """放行并注入上下文（exit 0）。context 为空时只保留 continue/meta。"""
    payload: dict[str, Any] = {"continue": True}
    context = context or ""
    if context:
        payload.update(_context_payload(event, context))
    meta = dict(meta or {})
    if meta:
        payload["specflow"] = meta
    return HookOutcome(
        payload=payload,
        exit_code=EXIT_OK,
        decision="allow",
        rule_id=rule_id,
        context_tokens=estimate_tokens(context) if context else 0,
        artifact_refs=[str(item) for item in meta.get("artifact_refs", []) or []],
    )


def block(
    reason: str,
    *,
    howto: str | None = None,
    meta: dict[str, Any] | None = None,
    exit_code: int = EXIT_BLOCK,
    rule_id: str | None = None,
    decision: str = "block",
) -> HookOutcome:
    """硬阻断（默认 exit 2）：stdout JSON `continue=false` + stderr 单行。"""
    full_reason = _compose_reason(reason, howto)
    meta = dict(meta or {})
    if howto:
        meta.setdefault("howto", howto)
    payload: dict[str, Any] = {
        "continue": False,
        "decision": "block",
        "reason": full_reason,
    }
    if meta:
        payload["specflow"] = meta
    return HookOutcome(
        payload=payload,
        exit_code=int(exit_code),
        stderr=full_reason,
        decision=decision,
        rule_id=rule_id,
    )


def needs_human(
    reason: str,
    *,
    howto: str | None = None,
    meta: dict[str, Any] | None = None,
) -> HookOutcome:
    """连续门控失败等场景：exit 3，提示停止自动重试、交人工。"""
    extra = dict(meta or {})
    extra.setdefault("needs_human", True)
    return block(
        reason,
        howto=howto,
        meta=extra,
        exit_code=EXIT_NEEDS_HUMAN,
        decision="needs_human",
    )


def allow(event: str, *, context: str = "", meta: dict[str, Any] | None = None) -> HookOutcome:
    """显式放行（PreToolUse 之外的 allow 语义；等价于空注入的 inject）。"""
    return inject(event, context, meta=meta)


# ---------------------------------------------------------------------- #
# 输出到宿主
# ---------------------------------------------------------------------- #
def _write_stdout(text: str) -> None:
    """按 UTF-8 写 stdout（宿主协议统一 UTF-8，避免 Windows GBK 管道二次转码）。"""
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        try:
            buffer.write((text + "\n").encode("utf-8"))
            buffer.flush()
            return
        except (OSError, ValueError):  # pragma: no cover - 平台相关
            pass
    try:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    except UnicodeEncodeError:  # 控制台非 UTF-8 时退化为 ASCII 转义，保证协议不炸
        sys.stdout.write(json.dumps(json.loads(text), ensure_ascii=True) + "\n")
        sys.stdout.flush()


def _write_stderr(text: str) -> None:
    """按 UTF-8 写 stderr（阻断理由进入模型上下文，必须与 stdout 编码一致）。"""
    buffer = getattr(sys.stderr, "buffer", None)
    if buffer is not None:
        try:
            buffer.write((text + "\n").encode("utf-8"))
            buffer.flush()
            return
        except (OSError, ValueError):  # pragma: no cover - 平台相关
            pass
    try:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()
    except UnicodeEncodeError:  # pragma: no cover - 极端控制台
        sys.stderr.write(text.encode("ascii", "replace").decode("ascii") + "\n")
        sys.stderr.flush()


def write_outcome(outcome: HookOutcome) -> int:
    """把 HookOutcome 写到 stdout/stderr，返回退出码。"""
    payload = outcome.payload if isinstance(outcome.payload, dict) else {}
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):  # pragma: no cover - 防御
        text = json.dumps({"continue": True, "specflow": {"error": "payload 不可序列化"}},
                          ensure_ascii=False)
    _write_stdout(text)
    if outcome.stderr:
        _write_stderr(_stderr_line(outcome.stderr))
    return int(outcome.exit_code)