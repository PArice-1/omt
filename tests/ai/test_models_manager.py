"""测试对象：src/omt/ai/models_manager.py::models_provider。

覆盖内容：
1. deepseek 注册项返回可调用模型，模型名称和 args 中的选项正确传递。
2. 未知 provider 拒绝初始化。
3. deepseek_init 的参数校验错误转换成带 [参数错误] 的 ValueError，并保留原因。
4. 空密钥及外部初始化错误继续向上传播，不作为普通返回值返回。

隔离方式：执行真实注册表与 deepseek_init，仅模拟外部 init_chat_model。
"""

from unittest.mock import Mock

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from pydantic import HttpUrl, SecretStr, ValidationError

from omt.ai.models_manager import models_provider


def test_registered_provider_returns_usable_model(
    chat_model_factory: Mock, chat_model: FakeListChatModel
):
    args = {"api_key": SecretStr("test-not-a-real-api-key"), "temperature": 0}

    result = models_provider(
        "deepseek",
        model="deepseek-chat",
        base_url=HttpUrl("https://api.deepseek.com"),
        args=args,
    )

    assert result is chat_model
    assert result.invoke("测试输入").content == "这是本地测试回复"
    chat_model_factory.assert_called_once_with(
        model_provider="deepseek",
        model="deepseek-chat",
        base_url="https://api.deepseek.com/",
        api_key=SecretStr("test-not-a-real-api-key"),
        temperature=0,
    )
    assert args == {"api_key": SecretStr("test-not-a-real-api-key"), "temperature": 0}


@pytest.mark.parametrize("provider", ["unknown-provider", ""])
def test_unknown_provider_is_rejected(provider: str, chat_model_factory: Mock):
    with pytest.raises(ValueError, match="模型未找到"):
        models_provider(
            provider,
            model="deepseek-chat",
            base_url=HttpUrl("https://api.deepseek.com"),
            args={"api_key": "test-not-a-real-api-key"},
        )

    chat_model_factory.assert_not_called()


@pytest.mark.parametrize("invalid_args", [{}, {"api_key": None}])
def test_validation_error_is_wrapped_with_original_cause(
    invalid_args: dict, chat_model_factory: Mock
):
    with pytest.raises(ValueError, match=r"\[参数错误\]") as error:
        models_provider(
            "deepseek",
            model="deepseek-chat",
            base_url=HttpUrl("https://api.deepseek.com"),
            args=invalid_args,
        )

    assert isinstance(error.value.__cause__, ValidationError)
    assert error.value.__cause__.errors()[0]["loc"] == ("api_key",)
    chat_model_factory.assert_not_called()


def test_blank_api_key_remains_value_error(chat_model_factory: Mock):
    with pytest.raises(ValueError, match="API_KEY 不能为空") as error:
        models_provider(
            "deepseek",
            model="deepseek-chat",
            base_url=HttpUrl("https://api.deepseek.com"),
            args={"api_key": " "},
        )

    assert not isinstance(error.value, ValidationError)
    assert error.value.__cause__ is None
    chat_model_factory.assert_not_called()


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_external_error_propagates_without_replacement(
    error_type: type[Exception], chat_model_factory: Mock
):
    original_error = error_type("外部模型初始化失败")
    chat_model_factory.side_effect = original_error

    with pytest.raises(error_type) as error:
        models_provider(
            "deepseek",
            model="deepseek-chat",
            base_url=HttpUrl("https://api.deepseek.com"),
            args={"api_key": "test-not-a-real-api-key"},
        )

    assert error.value is original_error
