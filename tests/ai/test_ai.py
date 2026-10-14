"""测试对象：src/omt/ai/ai.py::Model（__init__、init_model）。

覆盖内容：
1. 构造函数立即初始化模型，保存 provider/base_url，self.model 是可调用模型。
2. kwargs 经 models_provider/deepseek_init 传给外部模型初始化函数。
3. init_model 支持显式参数并返回模型实例。
4. 未知提供商、缺失密钥、空密钥和外部初始化失败会使构造失败，而非存入错误对象。

隔离方式：保留 Model → models_provider → deepseek_init 的真实调用链；
仅模拟外部 init_chat_model，不访问网络或项目凭据文件。
"""

from unittest.mock import Mock

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from pydantic import HttpUrl, SecretStr, ValidationError

from omt.ai.ai import Model


def test_constructor_initializes_and_stores_usable_model(
    chat_model_factory: Mock, chat_model: FakeListChatModel
):
    base_url = HttpUrl("https://api.deepseek.com")
    client = Model(
        "deepseek",
        model="deepseek-chat",
        base_url=base_url,
        api_key=SecretStr("test-not-a-real-api-key"),
        temperature=0,
    )

    assert client.provider == "deepseek"
    assert client.base_url == base_url
    assert client.model is chat_model
    assert client.model.invoke("测试输入").content == "这是本地测试回复"
    chat_model_factory.assert_called_once_with(
        model_provider="deepseek",
        model="deepseek-chat",
        base_url="https://api.deepseek.com/",
        api_key=SecretStr("test-not-a-real-api-key"),
        temperature=0,
    )


def test_init_model_returns_model_using_explicit_parameters(
    chat_model_factory: Mock, chat_model: FakeListChatModel
):
    client = Model(
        "deepseek",
        model="deepseek-chat",
        base_url=HttpUrl("https://api.deepseek.com"),
        api_key="test-not-a-real-api-key",
    )
    chat_model_factory.reset_mock()

    result = client.init_model(
        "deepseek",
        model="deepseek-reasoner",
        base_url=HttpUrl("https://api.deepseek.com/v1"),
        args={"api_key": "another-test-only-key", "max_tokens": 256},
    )

    assert result is chat_model
    chat_model_factory.assert_called_once_with(
        model_provider="deepseek",
        model="deepseek-reasoner",
        base_url="https://api.deepseek.com/v1",
        api_key=SecretStr("another-test-only-key"),
        max_tokens=256,
    )


def test_constructor_rejects_unknown_provider(chat_model_factory: Mock):
    with pytest.raises(ValueError, match="模型未找到"):
        Model(
            "unknown-provider",
            model="deepseek-chat",
            base_url=HttpUrl("https://api.deepseek.com"),
            api_key="test-not-a-real-api-key",
        )

    chat_model_factory.assert_not_called()


def test_constructor_rejects_missing_api_key(chat_model_factory: Mock):
    with pytest.raises(ValueError, match=r"\[参数错误\]") as error:
        Model(
            "deepseek",
            model="deepseek-chat",
            base_url=HttpUrl("https://api.deepseek.com"),
        )

    assert isinstance(error.value.__cause__, ValidationError)
    chat_model_factory.assert_not_called()


def test_constructor_rejects_blank_api_key(chat_model_factory: Mock):
    with pytest.raises(ValueError, match="API_KEY 不能为空"):
        Model(
            "deepseek",
            model="deepseek-chat",
            base_url=HttpUrl("https://api.deepseek.com"),
            api_key=" ",
        )

    chat_model_factory.assert_not_called()


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_constructor_propagates_external_initialization_error(
    error_type: type[Exception], chat_model_factory: Mock
):
    original_error = error_type("外部模型初始化失败")
    chat_model_factory.side_effect = original_error

    with pytest.raises(error_type) as error:
        Model(
            "deepseek",
            model="deepseek-chat",
            base_url=HttpUrl("https://api.deepseek.com"),
            api_key="test-not-a-real-api-key",
        )

    assert error.value is original_error
