"""LLM 层单测（docs/07 §5.7）：mock 确定性 + live 适配（monkeypatch urllib，不跑真实网络）。"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from specflow.config import Settings
from specflow.errors import LLMError, UsageError
from specflow.jsonschema import validate as json_validate
from specflow.llm import (
    MockLLM,
    OpenAICompatClient,
    get_llm,
    reset_llm,
    set_llm_for_tests,
)
from specflow.llm import mock_data
from specflow.output_schemas import OUTPUT_SCHEMAS

CHAT_URL = "https://api.example.test/v1/chat/completions"


# ---------------------------------------------------------------------- #
# 帮助函数
# ---------------------------------------------------------------------- #
def _settings(tmp_path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "LLM_MODE": "mock",
        "PLATFORM_MODE": "mock",
        "LLM_BASE_URL": "https://api.example.test/v1",
        "LLM_API_KEY": "",
        "LLM_MODEL": "deepseek-chat",
        "LLM_JSON_MODE": "1",
        "LLM_CACHE": "0",
        "LLM_TEMPERATURE": "0.3",
        "LLM_TIMEOUT_S": "5",
    }
    base.update(overrides)
    return Settings.load(project_dir=tmp_path, overrides=base)


class _FakeResponse(io.BytesIO):
    """最小 urlopen 响应替身：支持 `with` / read() / status。"""

    def __init__(self, payload: Any, status: int = 200) -> None:
        if isinstance(payload, (bytes, bytearray)):
            raw = bytes(payload)
        else:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        super().__init__(raw)
        self.status = status
        self.headers = {"Content-Type": "application/json"}


def _chat_payload(content: str, model: str = "deepseek-chat") -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


def _http_error(status: int, body: str = "server error") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        CHAT_URL,
        status,
        "error",
        {"Content-Type": "application/json"},
        io.BytesIO(body.encode("utf-8")),
    )


def _install_urlopen(monkeypatch, script: list[Any]) -> list[dict[str, Any]]:
    """按 script 顺序返回伪响应（异常直接 raise）；最后一个元素用于后续所有重试。"""
    calls: list[dict[str, Any]] = []

    def fake_urlopen(request, timeout=None, *args, **kwargs):  # noqa: ANN001
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        calls.append(
            {
                "url": request.full_url,
                "body": body,
                "timeout": timeout,
                "headers": {str(key).lower(): value for key, value in request.header_items()},
            }
        )
        item = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(item, BaseException):
            raise item
        return item if callable(item) else _FakeResponse(item)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def _answer_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["answer"],
        "properties": {"answer": {"type": "integer"}},
    }


# ---------------------------------------------------------------------- #
# mock LLM
# ---------------------------------------------------------------------- #
def test_mock_covers_all_output_schemas():
    assert set(OUTPUT_SCHEMAS) <= set(mock_data.RESPONSES)
    for scenario, schema in OUTPUT_SCHEMAS.items():
        default = mock_data.RESPONSES[scenario]["default"]
        assert json_validate(default, schema) == [], f"{scenario} 的 default 不满足 OUTPUT_SCHEMAS"
        result = MockLLM().complete(
            [{"role": "user", "content": "hi"}], schema=schema, scenario=scenario
        )
        assert result.mode == "mock"
        assert result.data == default
        assert json_validate(result.data, schema) == []


def test_mock_requires_scenario():
    with pytest.raises(UsageError, match="scenario"):
        MockLLM().complete([{"role": "user", "content": "hi"}])


def test_mock_unknown_scenario_raises():
    with pytest.raises(UsageError, match="scenario"):
        MockLLM().complete([{"role": "user", "content": "hi"}], scenario="no-such-scenario")


def test_mock_round_selection_and_defect_script():
    client = MockLLM()
    messages = [{"role": "user", "content": "hi"}]
    schema = OUTPUT_SCHEMAS["tdd-impl"]
    round1 = client.complete(messages, schema=schema, scenario="tdd-impl", round=1)
    round2 = client.complete(messages, schema=schema, scenario="tdd-impl", round=2)
    impl1 = round1.data["files"][0]["content"]
    impl2 = round2.data["files"][0]["content"]
    assert impl1 != impl2
    assert "seen" not in impl1 and "seen" in impl2

    tests1 = client.complete(messages, schema=OUTPUT_SCHEMAS["tdd-tests"], scenario="tdd-tests", round=1)
    tests2 = client.complete(messages, schema=OUTPUT_SCHEMAS["tdd-tests"], scenario="tdd-tests", round=2)
    assert tests1.data["cases"] == 5
    assert tests2.data["cases"] == 7
    assert tests1.data["files"][0]["content"] != tests2.data["files"][0]["content"]


def test_mock_returns_deepcopy():
    client = MockLLM()
    schema = OUTPUT_SCHEMAS["tdd-impl"]
    first = client.complete([{"role": "user", "content": "x"}], schema=schema, scenario="tdd-impl", round=2)
    first.data["files"][0]["content"] = "TAMPERED"
    second = client.complete([{"role": "user", "content": "x"}], schema=schema, scenario="tdd-impl", round=2)
    assert second.data["files"][0]["content"] != "TAMPERED"
    assert second.data["files"][0]["content"] == mock_data.RESPONSES["tdd-impl"][2]["files"][0]["content"]


def test_mock_schema_mismatch_surfaces_fixture_problem():
    broken_schema = {"type": "object", "required": ["definitely_missing"]}
    with pytest.raises(UsageError, match="JSON Schema"):
        MockLLM().complete(
            [{"role": "user", "content": "x"}], schema=broken_schema, scenario="tdd-impl"
        )


def test_mock_never_touches_network(monkeypatch):
    def _boom(*args, **kwargs):  # noqa: ANN001
        raise AssertionError("mock LLM 不应发起任何网络请求")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    result = MockLLM().complete(
        [{"role": "user", "content": "x"}],
        schema=OUTPUT_SCHEMAS["tdd-plan"],
        scenario="tdd-plan",
        round=1,
    )
    assert result.elapsed_ms == 0
    assert result.data is not None


def test_get_llm_respects_mode_and_test_override(tmp_path):
    mock_settings = _settings(tmp_path)
    assert isinstance(get_llm(mock_settings), MockLLM)

    live_settings = _settings(tmp_path, LLM_MODE="live", LLM_API_KEY="test-key")
    assert isinstance(get_llm(live_settings), OpenAICompatClient)

    fake = MockLLM(model="injected")
    set_llm_for_tests(fake)
    try:
        assert get_llm(live_settings) is fake
    finally:
        reset_llm()
    assert isinstance(get_llm(live_settings), OpenAICompatClient)


# ---------------------------------------------------------------------- #
# live 适配：请求构造与解析
# ---------------------------------------------------------------------- #
def test_live_posts_chat_completions(monkeypatch, tmp_path):
    settings = _settings(tmp_path, LLM_MODE="live", LLM_API_KEY="test-key")
    calls = _install_urlopen(monkeypatch, [_chat_payload('{"answer": 42}')])

    result = OpenAICompatClient(settings).complete(
        [{"role": "user", "content": "ping"}], schema=_answer_schema()
    )

    assert result.data == {"answer": 42}
    assert result.mode == "live"
    assert result.model == "deepseek-chat"
    assert result.usage["total_tokens"] == 18
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == CHAT_URL
    assert call["timeout"] == 5.0
    assert call["headers"]["authorization"] == "Bearer test-key"
    body = call["body"]
    assert body["model"] == "deepseek-chat"
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "system"
    assert "只输出 JSON" in body["messages"][0]["content"]
    assert body["messages"][-1] == {"role": "user", "content": "ping"}


def test_live_strips_json_code_fence(monkeypatch, tmp_path):
    settings = _settings(tmp_path, LLM_MODE="live", LLM_API_KEY="k")
    fenced = '```json\n{"answer": 5}\n```'
    _install_urlopen(monkeypatch, [_chat_payload(fenced)])
    result = OpenAICompatClient(settings).complete(
        [{"role": "user", "content": "x"}], schema=_answer_schema()
    )
    assert result.data == {"answer": 5}


def test_live_without_schema_keeps_text(monkeypatch, tmp_path):
    settings = _settings(tmp_path, LLM_MODE="live", LLM_API_KEY="k")
    calls = _install_urlopen(monkeypatch, [_chat_payload("plain text answer")])
    result = OpenAICompatClient(settings).complete([{"role": "user", "content": "x"}])
    assert result.data is None
    assert result.text == "plain text answer"
    assert "response_format" not in calls[0]["body"]
    assert len(calls[0]["body"]["messages"]) == 1


# ---------------------------------------------------------------------- #
# live 适配：JSON 纠错重试
# ---------------------------------------------------------------------- #
def test_live_retries_once_on_invalid_json(monkeypatch, tmp_path):
    settings = _settings(tmp_path, LLM_MODE="live", LLM_API_KEY="k")
    calls = _install_urlopen(
        monkeypatch,
        [_chat_payload("这不是 JSON"), _chat_payload('{"answer": 7}')],
    )
    result = OpenAICompatClient(settings).complete(
        [{"role": "user", "content": "x"}], schema=_answer_schema()
    )
    assert result.data == {"answer": 7}
    assert len(calls) == 2
    followup = calls[1]["body"]["messages"]
    assert followup[0]["role"] == "system"
    assert followup[1] == {"role": "user", "content": "x"}
    assert followup[2] == {"role": "assistant", "content": "这不是 JSON"}
    assert followup[3]["role"] == "user" and "不合格" in followup[3]["content"]


def test_live_retries_once_on_schema_mismatch(monkeypatch, tmp_path):
    settings = _settings(tmp_path, LLM_MODE="live", LLM_API_KEY="k")
    calls = _install_urlopen(
        monkeypatch,
        [_chat_payload('{"answer": "不是整数"}'), _chat_payload('{"answer": 9}')],
    )
    result = OpenAICompatClient(settings).complete(
        [{"role": "user", "content": "x"}], schema=_answer_schema()
    )
    assert result.data == {"answer": 9}
    assert len(calls) == 2
    correction = calls[1]["body"]["messages"][-1]["content"]
    assert "answer" in correction


def test_live_json_retry_exhausted_raises_llm_error(monkeypatch, tmp_path):
    settings = _settings(tmp_path, LLM_MODE="live", LLM_API_KEY="k")
    calls = _install_urlopen(
        monkeypatch, [_chat_payload("still not json"), _chat_payload("{oops}")]
    )
    with pytest.raises(LLMError, match="JSON"):
        OpenAICompatClient(settings).complete(
            [{"role": "user", "content": "x"}], schema=_answer_schema()
        )
    assert len(calls) == 2


# ---------------------------------------------------------------------- #
# live 适配：response_format 降级 / HTTP 重试 / 超时
# ---------------------------------------------------------------------- #
def test_live_degrades_response_format_on_400(monkeypatch, tmp_path):
    settings = _settings(tmp_path, LLM_MODE="live", LLM_API_KEY="k")
    calls = _install_urlopen(
        monkeypatch,
        [_http_error(400, "response_format is not supported"), _chat_payload('{"answer": 1}')],
    )
    result = OpenAICompatClient(settings).complete(
        [{"role": "user", "content": "x"}], schema=_answer_schema()
    )
    assert result.data == {"answer": 1}
    assert len(calls) == 2
    assert calls[0]["body"]["response_format"] == {"type": "json_object"}
    assert "response_format" not in calls[1]["body"]


def test_live_http_error_retries_once_then_llm_error(monkeypatch, tmp_path):
    settings = _settings(tmp_path, LLM_MODE="live", LLM_API_KEY="k")
    # 每次重试都用新的 HTTPError（HTTPError.read 只能消费一次 body）
    calls = _install_urlopen(
        monkeypatch,
        [_http_error(500, "boom-server-500"), _http_error(500, "boom-server-500")],
    )
    with pytest.raises(LLMError) as excinfo:
        OpenAICompatClient(settings).complete(
            [{"role": "user", "content": "x"}], schema=_answer_schema()
        )
    message = str(excinfo.value)
    assert "500" in message
    assert "boom-server-500" in message
    assert len(calls) == 2


def test_live_timeout_retries_once_then_llm_error(monkeypatch, tmp_path):
    settings = _settings(tmp_path, LLM_MODE="live", LLM_API_KEY="k")
    calls = _install_urlopen(monkeypatch, [TimeoutError("timed out")])
    with pytest.raises(LLMError, match="重试"):
        OpenAICompatClient(settings).complete(
            [{"role": "user", "content": "x"}], schema=_answer_schema()
        )
    assert len(calls) == 2


# ---------------------------------------------------------------------- #
# live 适配：缓存
# ---------------------------------------------------------------------- #
def test_live_cache_hit_avoids_second_request(monkeypatch, tmp_path):
    settings = _settings(
        tmp_path, LLM_MODE="live", LLM_API_KEY="k", LLM_CACHE="1"
    )
    calls = _install_urlopen(monkeypatch, [_chat_payload('{"answer": 3}')])
    client = OpenAICompatClient(settings)
    messages = [{"role": "user", "content": "cache-me"}]

    first = client.complete(messages, schema=_answer_schema())
    second = client.complete(messages, schema=_answer_schema())

    assert len(calls) == 1
    assert first.cached is False
    assert second.cached is True
    assert second.data == first.data == {"answer": 3}
    cache_files = list(settings.cache_dir.glob("*.json"))
    assert len(cache_files) == 1

    # 新客户端同样能命中（缓存按 model+messages+schema 持久化）
    third = OpenAICompatClient(settings).complete(messages, schema=_answer_schema())
    assert third.cached is True
    assert len(calls) == 1