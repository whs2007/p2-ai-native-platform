"""OpenAI 兼容的 live LLM 客户端（docs/07 §5.7）。

实现要点（与契约逐条对应）：
- `POST {base_url}/chat/completions`，仅用标准库 `urllib.request`（ADR-001）；
- 有 `schema` 时：system 追加「只输出 JSON」约束 + 请求体带
  `response_format={"type": "json_object"}`；若服务端 400/404/422 不支持该字段，
  自动去掉它重试一次；
- 解析 `choices[0].message.content`，去 ```json 围栏后做 JSON 解析与 schema 校验；
  校验失败把错误回填给模型纠错重试 **1 次**，二次失败抛 `LLMError`；
- HTTP 非 2xx / 超时：重试 1 次，仍失败抛 `LLMError`（含状态码与前 200 字）；
- `LLM_CACHE=1` 时按 `sha1(model + messages + schema)` 缓存到 `.spec/cache/llm/`。
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from typing import Any, Mapping

from specflow.errors import LLMError
from specflow.llm.base import (
    LLMResult,
    parse_json_object,
    validate_payload,
    with_json_instruction,
)
from specflow.util import atomic_write_text

__all__ = ["OpenAICompatClient"]

_JSON_MODE_FALLBACK_STATUS = (400, 404, 422)


class _HttpFailure(Exception):
    """内部用：把 HTTPError 规范化为 (状态码, 正文前 200 字)。"""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


class OpenAICompatClient:
    """live 模式：OpenAI / DeepSeek 兼容的 chat/completions 客户端。"""

    mode = "live"

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    # ------------------------------------------------------------------ #
    # 公共入口
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
        started = time.monotonic()
        prepared = (
            with_json_instruction(messages, schema)
            if schema is not None
            else [dict(message) for message in messages]
        )
        effective_temperature = (
            temperature if temperature is not None else float(self.settings.llm_temperature)
        )
        cache_key = self._cache_key(prepared, schema)

        if self._cache_enabled:
            cached = self._read_cache(cache_key, schema)
            if cached is not None:
                cached.cached = True
                cached.elapsed_ms = self._elapsed_ms(started)
                return cached

        conversation = list(prepared)
        response = self._request_chat(conversation, schema, effective_temperature)
        text = self._extract_content(response)
        data: dict[str, Any] | None = None

        if schema is not None:
            last_error: LLMError | None = None
            for attempt in range(2):
                try:
                    candidate = parse_json_object(text, source="live LLM")
                    errors = validate_payload(candidate, schema, source="live LLM")
                    if not errors:
                        data = candidate
                        break
                    last_error = LLMError("；".join(errors[:8]))
                except LLMError as exc:  # JSON 非法
                    last_error = exc
                if attempt == 1:
                    raise LLMError(
                        f"live LLM 输出未通过 JSON/schema 校验（已纠错重试 1 次）：{last_error}"
                    )
                conversation = conversation + [
                    {"role": "assistant", "content": text},
                    {
                        "role": "user",
                        "content": (
                            f"上一次输出不合格：{last_error}\n"
                            "请只输出一个严格符合给定 JSON Schema 的 JSON 对象，"
                            "不要输出解释、Markdown 代码围栏或任何额外文本。"
                        ),
                    },
                ]
                response = self._request_chat(conversation, schema, effective_temperature)
                text = self._extract_content(response)

        model = str(response.get("model") or self.settings.llm_model)
        usage = self._normalize_usage(response.get("usage"))
        result = LLMResult(
            text=text,
            data=data,
            usage=usage,
            model=model,
            mode=self.mode,
            elapsed_ms=self._elapsed_ms(started),
            cached=False,
        )
        if self._cache_enabled:
            self._write_cache(cache_key, result)
        return result

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #
    def _request_chat(
        self,
        messages: list[dict[str, str]],
        schema: Mapping[str, Any] | None,
        temperature: float,
    ) -> dict[str, Any]:
        use_response_format = schema is not None and bool(self.settings.llm_json_mode)
        transport_attempts = 0
        while True:
            payload = self._build_payload(messages, schema, temperature, use_response_format)
            try:
                return self._post_json(payload)
            except _HttpFailure as exc:
                if use_response_format and exc.status in _JSON_MODE_FALLBACK_STATUS:
                    # 服务端不支持 response_format：降级去掉该字段后再试
                    use_response_format = False
                    continue
                if transport_attempts >= 1:
                    raise LLMError(self._format_http_error(exc)) from exc
                transport_attempts += 1
            except (TimeoutError, OSError) as exc:
                if transport_attempts >= 1:
                    raise LLMError(
                        f"live LLM 请求失败（已重试 1 次）：{type(exc).__name__}: {exc}"
                    ) from exc
                transport_attempts += 1

    def _build_payload(
        self,
        messages: list[dict[str, str]],
        schema: Mapping[str, Any] | None,
        temperature: float,
        use_response_format: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if schema is not None and use_response_format:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _post_json(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        url = str(self.settings.llm_base_url).rstrip("/") + "/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        api_key = str(getattr(self.settings, "llm_api_key", "") or "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(
                request, timeout=float(self.settings.llm_timeout_s)
            ) as response:
                raw = response.read()
                status = int(getattr(response, "status", 200) or 200)
        except urllib.error.HTTPError as exc:
            raise _HttpFailure(exc.code, self._read_error_body(exc)) from exc
        if status >= 400:
            raise _HttpFailure(status, raw.decode("utf-8", errors="replace"))
        text = raw.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"live LLM 返回的响应不是合法 JSON：{text[:200]}") from exc
        if not isinstance(parsed, dict):
            raise LLMError(f"live LLM 响应顶层必须是对象，实际是 {type(parsed).__name__}")
        return parsed

    @staticmethod
    def _read_error_body(exc: urllib.error.HTTPError) -> str:
        try:
            raw = exc.read()
        except Exception:  # pragma: no cover - 防御性分支
            return str(exc)
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return str(raw)

    @staticmethod
    def _format_http_error(exc: _HttpFailure) -> str:
        preview = (exc.body or "").strip().replace("\n", " ")[:200]
        return f"live LLM HTTP {exc.status}：{preview}"

    @staticmethod
    def _extract_content(response: Mapping[str, Any]) -> str:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError("live LLM 响应缺少 choices（可能被服务端拒绝）")
        message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
        content = message.get("content") if isinstance(message, Mapping) else None
        if isinstance(content, str):
            return content
        if isinstance(content, list):  # 兼容分段 content
            parts = []
            for item in content:
                if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            if parts:
                return "".join(parts)
        raise LLMError("live LLM 响应 choices[0].message.content 为空或格式不支持")

    @staticmethod
    def _normalize_usage(raw: Any) -> dict[str, int]:
        usage = raw if isinstance(raw, Mapping) else {}
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        total = int(usage.get("total_tokens") or (prompt + completion))
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
        }

    # ------------------------------------------------------------------ #
    # 缓存（LLM_CACHE=1 时）
    # ------------------------------------------------------------------ #
    @property
    def _cache_enabled(self) -> bool:
        return bool(getattr(self.settings, "llm_cache", False))

    def _cache_key(
        self,
        messages: list[dict[str, str]],
        schema: Mapping[str, Any] | None,
    ) -> str:
        material = json.dumps(
            {
                "model": self.settings.llm_model,
                "messages": messages,
                "schema": schema,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha1(material.encode("utf-8")).hexdigest()

    def _cache_path(self, cache_key: str):
        cache_dir = self.settings.cache_dir
        return cache_dir / f"{cache_key}.json"

    def _read_cache(
        self,
        cache_key: str,
        schema: Mapping[str, Any] | None,
    ) -> LLMResult | None:
        path = self._cache_path(cache_key)
        try:
            raw = path.read_text(encoding="utf-8")
            entry = json.loads(raw)
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(entry, dict) or "text" not in entry:
            return None
        data = entry.get("data")
        if schema is not None:
            if data is None or validate_payload(data, schema, source="cache"):
                return None
        return LLMResult(
            text=str(entry.get("text") or ""),
            data=data if isinstance(data, dict) else None,
            usage=self._normalize_usage(entry.get("usage")),
            model=str(entry.get("model") or self.settings.llm_model),
            mode=self.mode,
            elapsed_ms=0,
            cached=True,
        )

    def _write_cache(self, cache_key: str, result: LLMResult) -> None:
        path = self._cache_path(cache_key)
        entry = {
            "text": result.text,
            "data": result.data,
            "usage": result.usage,
            "model": result.model,
            "mode": result.mode,
        }
        try:
            atomic_write_text(path, json.dumps(entry, ensure_ascii=False, indent=2) + "\n")
        except OSError:  # pragma: no cover - 缓存失败不影响主流程
            pass

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(0, int((time.monotonic() - started) * 1000))