"""测试对象：src/omt/ai/models/deepseek.py::deepseek_init。

覆盖内容：
1. 有效参数返回可调用模型，并向外部初始化函数传递 provider/model/URL/密钥/选项。
2. 字符串 URL 和密钥经 Pydantic 转换后仍能正常初始化。
3. 空密钥、空白密钥、非法 URL、错误参数类型和缺失密钥均拒绝初始化。
4. 外部初始化失败时异常原样传播。

隔离方式：仅模拟外部 init_chat_model，不请求 DeepSeek，不读取真实密钥。
"""

from unittest.mock import Mock

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from pydantic import HttpUrl, SecretStr, ValidationError

from omt.ai.models.deepseek import deepseek_init


@pytest.mark.parametrize("use_pydantic_types", [False, True])
def test_valid_parameters_return_usable_model(
    use_pydantic_types: bool,
    chat_model_factory: Mock,
    chat_model: FakeListChatModel,
):
    url = "https://api.deepseek.com/v1"
    key = "test-not-a-real-api-key"

    result = deepseek_init(
        "deepseek",
        model="deepseek-chat",
        base_url=HttpUrl(url) if use_pydantic_types else url,
        api_key=SecretStr(key) if use_pydantic_types else key,
        temperature=0,
        max_tokens=128,
    )

    assert result is chat_model
    assert result.invoke("测试输入").content == "这是本地测试回复"
    chat_model_factory.assert_called_once_with(
        model_provider="deepseek",
        model="deepseek-chat",
        base_url=url,
        api_key=SecretStr(key),
        temperature=0,
        max_tokens=128,
    )


@pytest.mark.parametrize("key", ["", " ", "\n\t"])
def test_blank_api_key_is_rejected(key: str, chat_model_factory: Mock):
    with pytest.raises(ValueError, match="API_KEY 不能为空"):
        deepseek_init(
            "deepseek",
            model="deepseek-chat",
            base_url="https://api.deepseek.com",
            api_key=SecretStr(key),
        )

    chat_model_factory.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("base_url", "not-a-url"),
        ("base_url", "ftp://example.com"),
        ("provider", None),
        ("model", None),
        ("api_key", None),
    ],
)
def test_invalid_parameters_are_rejected(
    field: str, value: object, chat_model_factory: Mock
):
    parameters = {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "api_key": "test-not-a-real-api-key",
    }
    parameters[field] = value

    with pytest.raises(ValidationError) as error:
        deepseek_init(**parameters)

    assert error.value.errors()[0]["loc"] == (field,)
    chat_model_factory.assert_not_called()


def test_missing_api_key_is_rejected(chat_model_factory: Mock):
    with pytest.raises(ValidationError) as error:
        deepseek_init(
            "deepseek",
            model="deepseek-chat",
            base_url="https://api.deepseek.com",
        )

    assert error.value.errors()[0]["loc"] == ("api_key",)
    chat_model_factory.assert_not_called()


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_external_initialization_error_propagates(
    error_type: type[Exception], chat_model_factory: Mock
):
    original_error = error_type("外部模型初始化失败")
    chat_model_factory.side_effect = original_error

    with pytest.raises(error_type) as error:
        deepseek_init(
            "deepseek",
            model="deepseek-chat",
            base_url="https://api.deepseek.com",
            api_key="test-not-a-real-api-key",
        )

    assert error.value is original_error
