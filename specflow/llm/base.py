"""LLM 客户端协议与通用工具（docs/07 §5.7）。

本模块只依赖标准库与 `specflow.{errors,jsonschema,util}`，不得 import
`agents` / `engine`（依赖方向见 07 §1）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from specflow.errors import LLMError
from specflow.util import estimate_tokens

__all__ = [
    "LLMResult",
    "LLMClient",
    "JSON_ONLY_INSTRUCTION",
    "json_only_instruction",
    "strip_code_fence",
    "parse_json_object",
    "validate_payload",
    "with_json_instruction",
    "build_usage",
]


@dataclass
class LLMResult:
    """一次 LLM 调用的统一结果。"""

    text: str
    data: dict[str, Any] | None
    usage: dict[str, int]
    model: str
    mode: str  # live | mock
    elapsed_ms: int
    cached: bool = False


@runtime_checkable
class LLMClient(Protocol):
    """LLM 客户端协议：live 与 mock 同签名（07 §5.7）。"""

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        schema: Mapping[str, Any] | None = None,
        scenario: str | None = None,
        round: int | None = None,
        temperature: float | None = None,
    ) -> LLMResult: ...


JSON_ONLY_INSTRUCTION = (
    "只输出 JSON：你的回复必须是一个合法的 JSON 对象，不要输出 Markdown 代码围栏、"
    "解释、前后缀说明或任何非 JSON 文本。字段必须严格符合给定的 JSON Schema，"
    "不得增删字段含义，不得输出阶段之外的内容。"
)

_FENCE_RE = re.compile(r"^```[A-Za-z0-9_-]*\s*\n(.*?)\n?```\s*$", re.DOTALL)


def json_only_instruction(schema: Mapping[str, Any] | None = None) -> str:
    """有 schema 时把 schema 本身也写进 system，降低模型跑偏概率。"""
    if schema is None:
        return JSON_ONLY_INSTRUCTION
    try:
        rendered = json.dumps(schema, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover - 防御性分支
        rendered = str(schema)
    return f"{JSON_ONLY_INSTRUCTION}\nJSON Schema：{rendered}"


def strip_code_fence(text: str) -> str:
    """去掉 ```json ... ``` 或 ``` ... ``` 围栏，返回裸 JSON 文本。"""
    stripped = (text or "").strip()
    if not stripped:
        return ""
    match = _FENCE_RE.match(stripped)
    if match:
        return match.group(1).strip()
    if stripped.startswith("```"):
        # 不完整的围栏：丢弃第一行，末尾 ``` 也丢弃
        first_newline = stripped.find("\n")
        stripped = stripped[first_newline + 1 :] if first_newline >= 0 else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
        return stripped.strip()
    return stripped


def parse_json_object(text: str, *, source: str = "LLM") -> dict[str, Any]:
    """解析模型输出为 JSON 对象；失败抛 LLMError（含前 200 字）。"""
    cleaned = strip_code_fence(text)
    preview = cleaned[:200]
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # 退一步：截取第一个 { 到最后一个 }，容忍模型加了说明文字
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError as exc:
                raise LLMError(
                    f"{source} 返回的不是合法 JSON：{exc}；内容前 200 字：{preview}"
                ) from exc
        else:
            raise LLMError(f"{source} 返回的不是合法 JSON：内容前 200 字：{preview}")
    if not isinstance(data, dict):
        raise LLMError(
            f"{source} 返回的 JSON 顶层必须是对象，实际是 {type(data).__name__}；"
            f"内容前 200 字：{preview}"
        )
    return data


def validate_payload(
    data: Any,
    schema: Mapping[str, Any] | None,
    *,
    source: str = "LLM",
) -> list[str]:
    """按项目内 JSON Schema 校验，返回错误列表（空 = 通过）。"""
    if schema is None:
        return []
    from specflow.jsonschema import validate as js_validate

    return js_validate(data, schema)


def with_json_instruction(
    messages: Sequence[Mapping[str, str]],
    schema: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    """有 schema 时在 system 消息末尾追加“只输出 JSON”的硬约束。"""
    prepared = [dict(message) for message in messages]
    instruction = json_only_instruction(schema)
    if prepared and prepared[0].get("role") == "system":
        system = str(prepared[0].get("content") or "").rstrip()
        prepared[0]["content"] = f"{system}\n\n{instruction}" if system else instruction
    else:
        prepared.insert(0, {"role": "system", "content": instruction})
    return prepared


def build_usage(messages: Sequence[Mapping[str, str]], text: str) -> dict[str, int]:
    """无真实 usage 时的确定性估算（mock 与缓存回放共用）。"""
    prompt_text = "\n".join(str(message.get("content") or "") for message in messages)
    prompt_tokens = estimate_tokens(prompt_text)
    completion_tokens = estimate_tokens(text)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }