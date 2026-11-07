"""平台适配层入口（docs/07 §5.9）。

    from specflow.platforms import get_platform
    platform = get_platform(settings)
    platform.whoami()

`PLATFORM_MODE=mock` → 本地 JSON（离线演示）；`PLATFORM_MODE=live` → HTTP REST
（`PLATFORM_BASE_URL` + `PLATFORM_TOKEN`）。调用方（S1/S4/S6 handler）无需感知实现差异。
"""
from __future__ import annotations

from specflow.config import Settings
from specflow.errors import UsageError
from specflow.platforms.base import PlatformAdapter
from specflow.platforms.http import HttpPlatform
from specflow.platforms.mock import MockPlatform

__all__ = ["PlatformAdapter", "HttpPlatform", "MockPlatform", "get_platform"]


def get_platform(settings: Settings) -> PlatformAdapter:
    """按 `settings.platform_mode` 返回平台适配器。"""
    mode = (settings.platform_mode or "mock").strip().lower()
    if mode == "mock":
        return MockPlatform(settings)
    if mode == "live":
        return HttpPlatform(settings)
    raise UsageError(f"未实现的平台模式：{mode}（可选 mock/live）")
