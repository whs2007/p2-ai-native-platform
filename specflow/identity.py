"""操作人身份解析：`--as` 优先；live 平台用 SSO `whoami`；mock 用 `SPECFLOW_OWNER`。

对应简历口径“基于 SSO 统一认证复用全流程身份”：`PLATFORM_MODE=live` 时，门控默认
操作人来自平台 `whoami()`，不再依赖本地默认值；显式 `--as` 仍可覆盖（审计/代操作）。
"""
from __future__ import annotations

from specflow.config import Settings
from specflow.errors import PlatformError
from specflow.platforms import get_platform


def resolve_operator(settings: Settings, explicit: str | None = None) -> str:
    candidate = (explicit or "").strip()
    if candidate:
        return candidate
    if settings.platform_mode == "live":
        user = get_platform(settings).whoami()
        name = str(user.get("name") or user.get("user_id") or "").strip()
        if not name:
            raise PlatformError("SSO whoami 未返回 name/user_id，无法记录门控操作人")
        return name
    return settings.owner