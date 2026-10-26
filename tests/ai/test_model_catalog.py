"""厂商目录适配只使用内存 HTTP 响应，验证用户地址路由、限制和失败清理。"""

import io
from email.message import Message
from http.client import IncompleteRead
from typing import override
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest
from pydantic import HttpUrl, SecretStr

from omt.ai.models import deepseek
from omt.ai.models_manager import list_provider_models, supported_providers


@pytest.mark.parametrize("base_url", ["https://api.deepseek.com", "https://example.test/proxy/v1/"])
def test_catalog_uses_provider_default_or_explicit_low_level_prefix(
    monkeypatch: pytest.MonkeyPatch, base_url: str,
) -> None:
    opener = Mock()
    response = io.BytesIO(b'{"data":[{"id":"second"},{"id":"first"},{"id":"second"}]}')
    opener.open.return_value = response
    monkeypatch.setattr(deepseek, "build_opener", Mock(return_value=opener))
    if base_url == "https://api.deepseek.com":
        result = list_provider_models(provider="deepseek", api_key=SecretStr("fake-api-key"))
    else:
        result = deepseek.list_deepseek_models(api_key=SecretStr("fake-api-key"), base_url=HttpUrl(base_url))
    assert result == ["first", "second"]
    request = opener.open.call_args.args[0]
    assert request.full_url == base_url.rstrip("/") + "/models"
    assert request.get_header("Authorization") == "Bearer fake-api-key"
    assert opener.open.call_args.kwargs["timeout"] == 10
    assert response.closed
    assert supported_providers() == ("deepseek", "ollama")


@pytest.mark.parametrize("payload", [b'{"data":[]}', b'{"data":[{"id":42}]}',
                                    b'fake-secret-marker', b'x' * (1024 * 1024 + 1)])
def test_invalid_catalog_response_is_sanitized_and_closed(
    payload: bytes, monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = Mock()
    response = io.BytesIO(payload)
    opener.open.return_value = response
    monkeypatch.setattr(deepseek, "build_opener", Mock(return_value=opener))
    with pytest.raises(ValueError) as raised:
        _ = deepseek.list_deepseek_models(api_key=SecretStr("fake-secret-marker"), base_url=HttpUrl("https://example.test"))
    assert "fake-secret-marker" not in str(raised.value)
    assert response.closed


@pytest.mark.parametrize("failure", ["http", "network", "read"])
def test_catalog_errors_hide_details_and_close_http_resources(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    class BrokenResponse(io.BytesIO):
        @override
        def read(self, size: int | None = -1) -> bytes:
            raise IncompleteRead(b"fake-secret-marker")

    response = BrokenResponse() if failure == "read" else io.BytesIO(b"fake-secret-marker")
    opener = Mock()
    if failure == "http":
        opener.open.side_effect = HTTPError("https://example.test/models", 401, "fake-secret-marker", Message(), response)
    elif failure == "network":
        opener.open.side_effect = URLError("fake-secret-marker")
    else:
        opener.open.return_value = response
    builder = Mock(return_value=opener)
    monkeypatch.setattr(deepseek, "build_opener", builder)
    with pytest.raises(ValueError) as raised:
        _ = deepseek.list_deepseek_models(api_key=SecretStr("fake-key"), base_url=HttpUrl("https://example.test"))
    assert "fake-secret-marker" not in str(raised.value)
    if failure != "network":
        assert response.closed
    handler = builder.call_args.args[0]
    assert handler.redirect_request(Mock(), io.BytesIO(), 302, "redirect", {}, "https://other.test") is None


def test_unregistered_provider_catalog_is_rejected() -> None:
    with pytest.raises(ValueError, match="未注册"):
        _ = list_provider_models(provider="unknown", api_key=SecretStr("fake-key"))
