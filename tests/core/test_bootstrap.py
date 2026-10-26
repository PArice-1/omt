"""共用组装测试：临时假配置与本地模型，CLI/API 不再依赖旧入口函数。"""

import asyncio
import json
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from pydantic import HttpUrl, SecretStr

from omt.agent.agent import Agent
from omt.core.bootstrap import build_agent as _get_agent, build_model as _get_model
from omt.core.settings import ModelDefinition, Settings, load_settings


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("defaultProvider", raising=False)
    monkeypatch.delenv("defaultModel", raising=False)
    monkeypatch.delenv("defaultThinkingLevel", raising=False)
    load_settings.cache_clear()
    directory = tmp_path / "agent"
    directory.mkdir()
    (directory / "settings.json").write_text(json.dumps({
        "defaultProvider": "deepseek", "defaultModel": "deepseek-chat",
    }), encoding="utf-8")
    (directory / "auth.json").write_text(json.dumps({
        "deepseek": {"type": "api_key", "key": "test-only-key"},
    }), encoding="utf-8")
    (directory / "model-store.json").write_text(json.dumps({
        "deepseek": {"models": [{
            "id": "deepseek-chat", "provider": "deepseek", "baseUrl": "https://example.test/v1",
            "thinkingLevelMap": {"media": "high"},
        }]},
    }), encoding="utf-8")
    yield directory
    load_settings.cache_clear()


@pytest.fixture
def model_factory(monkeypatch: pytest.MonkeyPatch) -> Mock:
    factory = Mock(return_value=FakeListChatModel(responses=["本地回复"]))
    monkeypatch.setattr(import_module("omt.ai.models.deepseek"), "init_chat_model", factory)
    return factory


def test_get_ollama_model_without_auth_file(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (config_dir / "auth.json").unlink()
    _ = (config_dir / "model-store.json").write_text(json.dumps({
        "ollama": {"models": [{"id": "qwen3:8b", "baseUrl": "http://localhost:11434"}]},
    }), encoding="utf-8")
    fake = FakeListChatModel(responses=["本地回复"])
    factory = Mock(return_value=fake)
    monkeypatch.setattr(import_module("omt.ai.models.ollama"), "init_chat_model", factory)
    client = _get_model(Settings(defaultProvider="ollama", defaultModel="qwen3:8b"))
    assert client.model is fake and fake.invoke("输入").content == "本地回复"
    options = factory.call_args.kwargs
    assert options["model_provider"] == "ollama" and options["model"] == "qwen3:8b"
    assert options["reasoning"] is False and "api_key" not in options
    assert not (config_dir / "auth.json").exists()


def test_get_model_joins_settings_auth_and_matching_catalog_entry(config_dir: Path, model_factory: Mock) -> None:
    client = _get_model(load_settings())

    assert client.provider == "deepseek"
    assert client.model is model_factory.return_value
    model_factory.assert_called_once_with(
        model_provider="deepseek",
        configurable_fields=None,
        config_prefix=None,
        model="deepseek-chat",
        base_url="https://example.test/v1",
        api_key=SecretStr("test-only-key"),
    )


@pytest.mark.parametrize("auth", [
    {},
    {"deepseek": {"type": "api_key"}},
    {"deepseek": {"type": "api_key", "key": ""}},
    {"deepseek": {"type": "api_key", "key": " \n\t"}},
])
def test_get_model_rejects_missing_or_blank_auth(
    config_dir: Path, model_factory: Mock, auth: dict[str, object],
) -> None:
    (config_dir / "auth.json").write_text(json.dumps(auth), encoding="utf-8")

    with pytest.raises(ValueError, match="认证|API Key"):
        _get_model(load_settings())

    model_factory.assert_not_called()


@pytest.mark.parametrize("auth_type", ["oauth", "bearer", "unknown"])
def test_get_model_explicitly_rejects_unsupported_auth_types(
    config_dir: Path, model_factory: Mock, auth_type: str,
) -> None:
    (config_dir / "auth.json").write_text(json.dumps({
        "deepseek": {"type": auth_type, "access": "test-token-not-an-api-key"},
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="认证类型.*不支持") as raised:
        _get_model(load_settings())

    assert "test-token-not-an-api-key" not in str(raised.value)
    model_factory.assert_not_called()


@pytest.mark.parametrize("catalog", [
    {},
    {"deepseek": {"models": []}},
    {"deepseek": {"models": [{"id": "different-model", "baseUrl": "https://example.test"}]}},
    {"deepseek": {"models": [{
        "id": "deepseek-chat", "provider": "wrong-provider", "baseUrl": "https://example.test",
    }]}},
])
def test_get_model_rejects_missing_or_mismatched_catalog(
    config_dir: Path, model_factory: Mock, catalog: dict[str, object],
) -> None:
    (config_dir / "model-store.json").write_text(json.dumps(catalog), encoding="utf-8")

    with pytest.raises(ValueError, match="model-store.json|提供商"):
        _get_model(load_settings())

    model_factory.assert_not_called()


def test_get_model_uses_url_of_exact_selected_model_not_first_entry(config_dir: Path, model_factory: Mock) -> None:
    (config_dir / "model-store.json").write_text(json.dumps({
        "deepseek": {"models": [
            {"id": "other", "baseUrl": "https://wrong.example.test"},
            {"id": "deepseek-chat", "baseUrl": "https://selected.example.test/v1",
             "thinkingLevelMap": {"media": "high"}},
        ]},
    }), encoding="utf-8")

    _get_model(load_settings())

    assert model_factory.call_args.kwargs["base_url"] == "https://selected.example.test/v1"


@pytest.mark.parametrize("level, effort", [("high", "max"), ("none", "high")])
def test_get_model_maps_selected_thinking_level(
    config_dir: Path, model_factory: Mock, level: str, effort: str,
) -> None:
    _ = (config_dir / "model-store.json").write_text(json.dumps({
        "deepseek": {"models": [{
            "id": "deepseek-chat", "baseUrl": "https://example.test/v1",
            "thinkingLevelMap": {"media": "high", level: effort},
        }]},
    }), encoding="utf-8")
    settings = Settings(defaultProvider="deepseek", defaultModel="deepseek-chat",
                        defaultThinkingLevel=level)

    client = _get_model(settings)

    assert client.model is model_factory.return_value
    options = model_factory.call_args.kwargs
    if level == "none":
        assert "reasoning_effort" not in options
        assert "extra_body" not in options
    else:
        assert options["reasoning_effort"] == effort
        assert options["extra_body"] == {"thinking": {"type": "enabled"}}


@pytest.mark.parametrize("metadata", [
    {}, {"thinkingLevelMap": None}, {"thinkingLevelMap": {}},
    {"thinkingLevelMap": {"high": "high"}}, {"thinkingLevelMap": {"media": None}},
])
@pytest.mark.parametrize("level", [None, "media", "none"])
def test_get_model_uses_none_when_thinking_is_unconfigured_or_unavailable(
    config_dir: Path, model_factory: Mock, metadata: dict[str, object], level: str | None,
) -> None:
    _ = (config_dir / "model-store.json").write_text(json.dumps({
        "deepseek": {"models": [{
            "id": "deepseek-chat", "baseUrl": "https://example.test/v1", **metadata,
        }]},
    }), encoding="utf-8")

    settings = Settings(defaultProvider="deepseek", defaultModel="deepseek-chat",
                        defaultThinkingLevel=level)

    client = _get_model(settings)

    assert client.model is model_factory.return_value
    options = model_factory.call_args.kwargs
    assert "reasoning_effort" not in options
    assert "extra_body" not in options


def test_get_model_rejects_unregistered_provider_before_initializing_model(config_dir: Path, model_factory: Mock) -> None:
    settings = Settings(defaultProvider="not-registered", defaultModel="some-model")

    with pytest.raises(ValueError, match="提供商.*未注册"):
        _get_model(settings)

    model_factory.assert_not_called()


def test_get_agent_uses_underlying_chat_model_and_builtin_tools(
    config_dir: Path, model_factory: Mock
) -> None:
    client = _get_model(load_settings())

    agent = asyncio.run(_get_agent(client))

    assert isinstance(agent, Agent)
    assert agent.model is client.model
    assert agent.tools is not None
    assert {getattr(item, "name", None) for item in agent.tools} == {"read", "edit", "write", "bash"}


@pytest.mark.parametrize("model_id, provider", [("wrong-model", "deepseek"), ("deepseek-chat", "wrong-provider")])
def test_get_model_rejects_mismatched_definition_snapshot(
    config_dir: Path, model_factory: Mock, model_id: str, provider: str,
) -> None:
    definition = ModelDefinition(
        id=model_id, provider=provider, baseUrl=HttpUrl("https://example.test/v1"),
        thinkingLevelMap={"high": "high"},
    )

    with pytest.raises(ValueError, match="与配置不一致"):
        _ = _get_model(load_settings(), definition=definition)

    model_factory.assert_not_called()
