"""异常与退出码约定（docs/07 §2.2）。

退出码：0 成功 · 2 门控/规则硬阻断 · 3 需人工介入 · 1 运行错误。
"""
from __future__ import annotations

from typing import Any, Iterable


class SpecflowError(Exception):
    """所有 specflow 自定义异常的基类。"""

    exit_code: int = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def render(self) -> str:
        if self.hint:
            return f"{self.message}\n提示：{self.hint}"
        return self.message


class UsageError(SpecflowError):
    """参数/用法错误。"""

    exit_code = 1


class GateBlocked(SpecflowError):
    """机器门或人门未通过：硬阻断，不调用 LLM。"""

    exit_code = 2

    def __init__(
        self,
        message: str,
        *,
        missing: Iterable[str] | None = None,
        reasons: Iterable[str] | None = None,
        howto: str | None = None,
        details: dict[str, Any] | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.missing = list(missing or [])
        self.reasons = list(reasons or [])
        self.howto = howto
        self.details = dict(details or {})

    def render(self) -> str:
        lines = [self.message]
        for item in self.missing:
            lines.append(f"  - {item}")
        if self.howto:
            lines.append(f"补齐方式：{self.howto}")
        for reason in self.reasons:
            lines.append(f"原因：{reason}")
        return "\n".join(lines)


class NeedsHuman(SpecflowError):
    """不阻断，但必须由人介入（写锁占用、连续门控失败、TDD 超限）。"""

    exit_code = 3


class LockBusy(NeedsHuman):
    pass


class LLMError(SpecflowError):
    pass


class PlatformError(SpecflowError):
    """平台适配层 HTTP/RPC 调用失败。"""


class ArtifactError(SpecflowError):
    pass


class ArtifactNotFound(ArtifactError):
    pass


class IllegalTransition(SpecflowError):
    pass


class HookError(SpecflowError):
    pass