"""LLM 适配层入口：`get_llm(settings)` + 测试注入点（docs/07 §5.7）。"""
from __future__ import annotations

from typing import Any

from specflow.config import Settings
from specflow.llm.base import LLMClient, LLMResult
from specflow.llm.mock import MockLLM
from specflow.llm.openai_compat import OpenAICompatClient

__all__ = [
    "LLMClient",
    "LLMResult",
    "MockLLM",
    "OpenAICompatClient",
    "get_llm",
    "set_llm_for_tests",
    "reset_llm",
]

_OVERRIDE: Any | None = None


def get_llm(settings: Settings) -> LLMClient:
    """按 `settings.effective_llm_mode` 返回 live 或 mock 客户端。

    live 只代表「配置为 live」；本机无 API Key 时 `auto` 仍会解析为 mock。
    测试可用 `set_llm_for_tests` 注入任意实现，`reset_llm` 清除。
    """
    if _OVERRIDE is not None:
        return _OVERRIDE
    mode = settings.effective_llm_mode
    if mode == "live":
        return OpenAICompatClient(settings)
    return MockLLM(settings)


def set_llm_for_tests(client: LLMClient | None) -> None:
    """测试专用：注入/清除全局 LLM 替身。"""
    global _OVERRIDE
    _OVERRIDE = client


def reset_llm() -> None:
    """测试专用：恢复按配置构造。"""
    set_llm_for_tests(None)