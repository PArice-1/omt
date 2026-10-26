"""Ollama 适配只使用假聊天模型、内存 HTTP 和 MockTransport，不连接真实服务。"""

import asyncio
import io
import json
from email.message import Message
from http.client import IncompleteRead
from typing import override
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from pydantic import HttpUrl, JsonValue, SecretStr, TypeAdapter, ValidationError

from omt.ai.model import Model
from omt.ai.models import ollama
from omt.ai.models_manager import list_provider_models, models, models_provider, supported_providers


@pytest.fixture
def ollama_factory(monkeypatch: pytest.MonkeyPatch, chat_model: FakeListChatModel) -> Mock:
    factory = Mock(return_value=chat_model)
    monkeypatch.setattr(ollama, "init_chat_model", factory)
    return factory


def test_registered_model_is_usable_without_api_key(
    ollama_factory: Mock, chat_model: FakeListChatModel,
) -> None:
    client = Model("ollama", model="qwen3:8b", base_url=ollama.OLLAMA_BASE_URL,
                   temperature=0, num_ctx=4096, num_predict=128)
    assert client.model is chat_model
    assert isinstance(client.model, BaseChatModel)
    assert client.model.invoke("本地输入").content == "这是本地测试回复"
    ollama_factory.assert_called_once_with(
        model_provider="ollama", model="qwen3:8b", base_url="http://localhost:11434/",
        configurable_fields=None, config_prefix=None, validate_model_on_init=False,
        temperature=0, num_ctx=4096, num_predict=128,
    )
    assert "ollama" in supported_providers()
    assert models["ollama"].base_url == ollama.OLLAMA_BASE_URL
    assert not models["ollama"].thinking_level_maps


@pytest.mark.parametrize("key", [None, SecretStr(""), SecretStr("test-only-key")])
def test_compatibility_key_is_not_forwarded(key: SecretStr | None, ollama_factory: Mock) -> None:
    _ = ollama.ollama_init(model="qwen3:8b", api_key=key)
    assert "api_key" not in ollama_factory.call_args.kwargs
    assert "reasoning_effort" not in ollama_factory.call_args.kwargs
    assert "reasoning" not in ollama_factory.call_args.kwargs


@pytest.mark.parametrize(("effort", "expected"), [
    (None, True), ("none", False), ("false", False), ("true", True),
    ("low", "low"), ("medium", "medium"), ("high", "high"),
])
def test_reasoning_effort_maps_to_native_option(
    effort: str | None, expected: bool | str, ollama_factory: Mock,
) -> None:
    options: dict[str, object] = {"reasoning": True, "num_predict": 64, "reasoning_effort": effort}
    _ = models_provider("ollama", model="local:latest", base_url=ollama.OLLAMA_BASE_URL, args=options)
    forwarded = ollama_factory.call_args.kwargs
    assert forwarded["reasoning"] == expected
    assert forwarded["num_predict"] == 64
    assert "reasoning_effort" not in forwarded and "extra_body" not in forwarded
    assert options == {"reasoning": True, "num_predict": 64, "reasoning_effort": effort}


@pytest.mark.parametrize("effort", ["max", "unknown", ""])
def test_unsupported_effort_is_rejected_before_sdk(effort: str, ollama_factory: Mock) -> None:
    with pytest.raises(ValueError, match="reasoning_effort 仅支持"):
        _ = ollama.ollama_init(model="local", reasoning_effort=effort)
    ollama_factory.assert_not_called()


@pytest.mark.parametrize("name", ["", " \n\t"])
def test_blank_model_is_rejected(name: str, ollama_factory: Mock) -> None:
    with pytest.raises(ValueError, match="模型名称不能为空"):
        _ = ollama.ollama_init(model=name)
    ollama_factory.assert_not_called()


@pytest.mark.parametrize(("field", "value"), [
    ("base_url", "not-a-url"), ("base_url", "ftp://example.test"), ("model", None),
])
def test_invalid_parameters_are_rejected(field: str, value: object, ollama_factory: Mock) -> None:
    parameters = {"model": "local", "base_url": "http://localhost:11434", field: value}
    with pytest.raises(ValidationError) as raised:
        _ = models["ollama"].initialize(**parameters)
    assert raised.value.errors()[0]["loc"] == (field,)
    ollama_factory.assert_not_called()


def test_explicit_validation_and_sdk_failure_are_preserved(ollama_factory: Mock) -> None:
    failure = RuntimeError("本地初始化失败")
    ollama_factory.side_effect = failure
    with pytest.raises(RuntimeError) as raised:
        _ = ollama.ollama_init(model="local", validate_model_on_init=True)
    assert raised.value is failure
    assert ollama_factory.call_args.kwargs["validate_model_on_init"] is True


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_real_sdk_uses_native_chat_and_separates_reasoning(
    mode: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "model": "local:latest", "message": {
                "role": "assistant", "content": "本地回答", "thinking": "独立思考",
            }, "done": True, "prompt_eval_count": 3, "eval_count": 4,
        })

    model = ollama.ollama_init(
        model="local:latest", base_url=HttpUrl("http://example.test/proxy/"),
        reasoning_effort="high", api_key=SecretStr("must-not-be-sent"),
        client_kwargs={"transport": httpx.MockTransport(respond)},
    )
    assert requests == []
    message = asyncio.run(model.ainvoke("输入")) if mode == "async" else model.invoke("输入")
    assert message.content == "本地回答"
    assert message.additional_kwargs["reasoning_content"] == "独立思考"
    assert len(requests) == 1 and requests[0].method == "POST"
    assert str(requests[0].url) == "http://example.test/proxy/api/chat"
    body = TypeAdapter(dict[str, JsonValue]).validate_json(requests[0].content)
    assert body["model"] == "local:latest" and body["think"] == "high"
    assert "authorization" not in requests[0].headers
    assert "api_key" not in body and "reasoning_effort" not in body


@pytest.mark.parametrize("base_url", [None, HttpUrl("http://example.test/proxy/")])
def test_catalog_uses_native_tags_and_preserves_full_names(
    base_url: HttpUrl | None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = io.BytesIO(json.dumps({"models": [
        {"name": " qwen3:8b "}, {"name": "local:latest"}, {"name": "qwen3:8b"}, {"name": " "},
    ]}).encode("utf-8"))
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(ollama, "build_opener", Mock(return_value=opener))
    if base_url is None:
        result = list_provider_models(provider="ollama", api_key=SecretStr("must-not-be-sent"))
        expected_url = "http://localhost:11434/api/tags"
    else:
        result = ollama.list_ollama_models(base_url=base_url)
        expected_url = "http://example.test/proxy/api/tags"
    assert result == ["local:latest", "qwen3:8b"]
    request = opener.open.call_args.args[0]
    assert request.full_url == expected_url
    assert request.get_header("Authorization") is None
    assert opener.open.call_args.kwargs["timeout"] == 10
    assert response.closed


@pytest.mark.parametrize("payload", [
    b'{"models":[]}', b'{"models":[{"name":" "}]}', b'{"models":[{"name":42}]}',
    b'{"data":[]}', b'fake-private-marker', b'x' * (1024 * 1024 + 1),
])
def test_catalog_invalid_responses_are_sanitized_and_closed(
    payload: bytes, monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = Mock()
    response = io.BytesIO(payload)
    opener.open.return_value = response
    monkeypatch.setattr(ollama, "build_opener", Mock(return_value=opener))
    with pytest.raises(ValueError, match="确认服务已启动并已下载模型") as raised:
        _ = ollama.list_ollama_models()
    assert "fake-private-marker" not in str(raised.value)
    assert response.closed


@pytest.mark.parametrize("failure", ["http", "network", "read"])
def test_catalog_errors_do_not_leak_and_http_resources_are_closed(
    failure: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenResponse(io.BytesIO):
        @override
        def read(self, size: int | None = -1) -> bytes:
            raise IncompleteRead(b"fake-private-marker")

    response = BrokenResponse() if failure == "read" else io.BytesIO(b"fake-private-marker")
    opener = Mock()
    if failure == "http":
        opener.open.side_effect = HTTPError("http://example.test/api/tags", 500,
                                           "fake-private-marker", Message(), response)
    elif failure == "network":
        opener.open.side_effect = URLError("fake-private-marker")
    else:
        opener.open.return_value = response
    builder = Mock(return_value=opener)
    monkeypatch.setattr(ollama, "build_opener", builder)
    with pytest.raises(ValueError) as raised:
        _ = ollama.list_ollama_models()
    assert "fake-private-marker" not in str(raised.value)
    if failure != "network":
        assert response.closed
    handler = builder.call_args.args[0]
    assert handler.redirect_request(Mock(), io.BytesIO(), 302, "redirect", {}, "http://other.test") is None


@pytest.mark.parametrize("url", [
    "http://user:secret@example.test", "http://example.test?key=secret", "http://example.test#secret",
])
def test_catalog_rejects_embedded_auth_query_and_fragment(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    builder = Mock()
    monkeypatch.setattr(ollama, "build_opener", builder)
    with pytest.raises(ValueError, match="不得包含认证") as raised:
        _ = ollama.list_ollama_models(base_url=HttpUrl(url))
    assert "secret" not in str(raised.value)
    builder.assert_not_called()
