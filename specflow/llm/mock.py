"""Mock LLM：按 scenario（+ round）查表返回预置 JSON（docs/07 §5.7）。

特性：
- 必须提供 `scenario`，缺失/未知均抛 `UsageError`（错误矩阵 07 §7）；
- `data` 用调用方传入的 schema 强校验，不合法直接抛（暴露 fixture 问题）；
- 返回深拷贝，调用方修改结果不会污染预置数据；
- 无网络、无 sleep、耗时恒为 0ms，保证测试与离线演示 100% 确定。
"""
from __future__ import annotations

import copy
import json
from typing import Any, Mapping

from specflow.errors import UsageError
from specflow.llm import mock_data
from specflow.llm.base import LLMResult, build_usage, validate_payload

__all__ = ["MockLLM", "resolve_response"]


def resolve_response(
    scenario: str,
    round_no: int | None,
    responses: Mapping[str, Mapping[Any, dict[str, Any]]] | None = None,
    *,
    source: str = "mock_data.RESPONSES",
) -> tuple[Any, dict[str, Any]]:
    """按 scenario + round 选条目；返回 (命中的 key, data 深拷贝)。"""
    table = responses or mock_data.RESPONSES
    if scenario not in table:
        available = "、".join(sorted(str(key) for key in table))
        raise UsageError(
            f"mock LLM 缺少 scenario：{scenario!r}（{source} 已登记：{available or '无'}）"
        )
    entries = table[scenario]
    if not isinstance(entries, Mapping) or not entries:
        raise UsageError(f"mock LLM 的 scenario {scenario!r} 没有可用响应")
    key: Any
    if round_no is not None and round_no in entries:
        key = round_no
    elif "default" in entries:
        key = "default"
    elif len(entries) == 1:
        key = next(iter(entries))
    else:
        rounds = "、".join(sorted(str(item) for item in entries))
        raise UsageError(
            f"mock LLM 的 scenario {scenario!r} 没有 round={round_no} 的响应"
            f"（可选：{rounds}）"
        )
    data = entries[key]
    if not isinstance(data, dict):
        raise UsageError(f"mock LLM 的 {scenario}[{key!r}] 必须是 JSON 对象")
    return key, copy.deepcopy(data)


class MockLLM:
    """确定性 LLM 客户端（mode=mock）。"""

    mode = "mock"

    def __init__(
        self,
        settings: Any | None = None,
        *,
        responses: Mapping[str, Mapping[Any, dict[str, Any]]] | None = None,
        model: str = "mock-llm",
    ) -> None:
        self.settings = settings
        self.responses = responses
        self.model = model
        self.calls: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        schema: Mapping[str, Any] | None = None,
        scenario: str | None = None,
        round: int | None = None,
        temperature: float | None = None,
    ) -> LLMResult:
        if not scenario:
            raise UsageError(
                "mock LLM 必须提供 scenario（runner 应按阶段传入，例如 'tech-design'、'tdd-impl'）"
            )
        key, data = resolve_response(scenario, round, self.responses)
        if schema is not None:
            errors = validate_payload(data, schema, source=f"mock:{scenario}[{key!r}]")
            if errors:
                raise UsageError(
                    f"mock 数据 {scenario}[{key!r}] 不符合给定 JSON Schema："
                    + "；".join(errors[:8])
                )
        text = json.dumps(data, ensure_ascii=False, sort_keys=True)
        usage = build_usage(messages, text)
        self.calls.append(
            {
                "scenario": scenario,
                "round": round,
                "cached": False,
                "schema": schema is not None,
                "messages": copy.deepcopy(messages),
                "temperature": temperature,
            }
        )
        return LLMResult(
            text=text,
            data=data,
            usage=usage,
            model=self.model,
            mode=self.mode,
            elapsed_ms=0,
            cached=False,
        )