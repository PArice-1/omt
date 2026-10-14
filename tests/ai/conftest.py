"""AI 测试的共享 fixture（本文件不包含测试用例）。

作用：只替换 src/omt/ai/models/deepseek.py 使用的外部 init_chat_model，
不替换 Model、models_provider 或 deepseek_init，保留真实的内部调用链。
返回本地 FakeListChatModel，禁止真实 API 请求；密钥均为无效测试占位值。
"""

from importlib import import_module
from unittest.mock import Mock

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel


@pytest.fixture
def chat_model() -> FakeListChatModel:
    return FakeListChatModel(responses=["这是本地测试回复"])


@pytest.fixture
def chat_model_factory(
    monkeypatch: pytest.MonkeyPatch,
    chat_model: FakeListChatModel,
) -> Mock:
    factory = Mock(return_value=chat_model)
    module = import_module("omt.ai.models.deepseek")
    monkeypatch.setattr(module, "init_chat_model", factory)
    return factory
