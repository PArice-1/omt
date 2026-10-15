"""会话组装和终端输入测试：假模型、临时配置、内存终端，不调用真实提供商。"""

import json
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel, FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from prompt_toolkit import PromptSession
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.validation import ValidationError as InputValidationError
from pydantic import SecretStr

from omt.agent.agent import Agent
from omt.coding_agent.session import (
    _get_agent,
    _get_model,
    _get_user_input,
    _UserInputValidator,
    session,
)
from omt.coding_agent.settings import Settings, load_settings


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("defaultProvider", raising=False)
    monkeypatch.delenv("defaultModel", raising=False)
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
        }]},
    }), encoding="utf-8")
    yield directory
    load_settings.cache_clear()


@pytest.fixture
def model_factory(monkeypatch: pytest.MonkeyPatch) -> Mock:
    factory = Mock(return_value=FakeListChatModel(responses=["本地回复"]))
    monkeypatch.setattr(import_module("omt.ai.models.deepseek"), "init_chat_model", factory)
    return factory


def test_get_model_joins_settings_auth_and_matching_catalog_entry(config_dir, model_factory):
    client = _get_model(load_settings())

    assert client.provider == "deepseek"
    assert client.model is model_factory.return_value
    model_factory.assert_called_once_with(
        model_provider="deepseek",
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
def test_get_model_rejects_missing_or_blank_auth(config_dir, model_factory, auth):
    (config_dir / "auth.json").write_text(json.dumps(auth), encoding="utf-8")

    with pytest.raises(ValueError, match="认证|API Key"):
        _get_model(load_settings())

    model_factory.assert_not_called()


@pytest.mark.parametrize("auth_type", ["oauth", "bearer", "unknown"])
def test_get_model_explicitly_rejects_unsupported_auth_types(config_dir, model_factory, auth_type):
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
def test_get_model_rejects_missing_or_mismatched_catalog(config_dir, model_factory, catalog):
    (config_dir / "model-store.json").write_text(json.dumps(catalog), encoding="utf-8")

    with pytest.raises(ValueError, match="model-store.json|提供商"):
        _get_model(load_settings())

    model_factory.assert_not_called()


def test_get_model_uses_url_of_exact_selected_model_not_first_entry(config_dir, model_factory):
    (config_dir / "model-store.json").write_text(json.dumps({
        "deepseek": {"models": [
            {"id": "other", "baseUrl": "https://wrong.example.test"},
            {"id": "deepseek-chat", "baseUrl": "https://selected.example.test/v1"},
        ]},
    }), encoding="utf-8")

    _get_model(load_settings())

    assert model_factory.call_args.kwargs["base_url"] == "https://selected.example.test/v1"


def test_get_model_rejects_unregistered_provider_before_initializing_model(config_dir, model_factory):
    settings = Settings(defaultProvider="not-registered", defaultModel="some-model")

    with pytest.raises(ValueError, match="提供商.*未注册"):
        _get_model(settings)

    model_factory.assert_not_called()


def test_get_agent_uses_underlying_chat_model_and_builtin_tools(config_dir, model_factory):
    client = _get_model(load_settings())

    agent = _get_agent(client)

    assert isinstance(agent, Agent)
    assert agent.model is client.model
    assert agent.tools is not None
    assert {getattr(item, "name", None) for item in agent.tools} == {"read", "edit", "write"}


def test_get_user_input_uses_real_prompt_session_without_builtin_input(monkeypatch):
    forbidden_input = Mock(side_effect=AssertionError("不得使用内置 input"))
    monkeypatch.setattr("builtins.input", forbidden_input)
    with create_pipe_input() as pipe:
        terminal = PromptSession[str](input=pipe, output=DummyOutput())
        pipe.send_text("  你好  \n")

        result = _get_user_input(terminal)

    assert result == "  你好  "
    forbidden_input.assert_not_called()


@pytest.mark.parametrize("value", ["", "   ", "\n\t", "hello\x00world"])
def test_input_validator_rejects_blank_or_nul_input(value):
    with pytest.raises(InputValidationError):
        _UserInputValidator().validate(Document(value))


def test_input_validator_accepts_unicode_and_multiline():
    _UserInputValidator().validate(Document("分析这个项目\n并列出问题"))


def test_prompt_rejects_blank_line_then_accepts_next_input():
    with create_pipe_input() as pipe:
        terminal = PromptSession[str](input=pipe, output=DummyOutput())
        pipe.send_text("\n你好\n")

        assert _get_user_input(terminal) == "你好"


@pytest.mark.parametrize(("control", "exception"), [("\x03", KeyboardInterrupt), ("\x04", EOFError)])
def test_prompt_propagates_cancel_and_eof(control, exception):
    with create_pipe_input() as pipe:
        terminal = PromptSession[str](input=pipe, output=DummyOutput())
        pipe.send_text(control)

        with pytest.raises(exception):
            _get_user_input(terminal)


@pytest.mark.parametrize("exit_command", ["/exit", "/quit", " /EXIT "])
def test_session_exits_without_sending_exit_command_to_model(
    config_dir, model_factory, monkeypatch, exit_command
):
    module = import_module("omt.coding_agent.session")
    runner = Mock()
    runner_factory = Mock(return_value=runner)
    monkeypatch.setattr(module, "CreateAgentSession", runner_factory)
    monkeypatch.setattr(module, "PromptSession", Mock())
    monkeypatch.setattr(module, "_get_user_input", Mock(return_value=exit_command))

    session()

    runner_factory.assert_called_once()
    runner.run.assert_not_called()


def test_session_handles_input_cancel_and_eof_and_does_not_print_answer_twice(
    config_dir, model_factory, monkeypatch, capsys
):
    module = import_module("omt.coding_agent.session")
    runner = Mock()
    runner.run.return_value = "只由 runtime 输出的回答"
    monkeypatch.setattr(module, "CreateAgentSession", Mock(return_value=runner))
    terminal_factory = Mock()
    monkeypatch.setattr(module, "PromptSession", terminal_factory)
    read_input = Mock(side_effect=[KeyboardInterrupt(), "你好", EOFError()])
    monkeypatch.setattr(module, "_get_user_input", read_input)

    session()

    runner.run.assert_called_once_with("你好")
    terminal_factory.assert_called_once()
    assert all(call.args == (terminal_factory.return_value,) for call in read_input.call_args_list)
    assert "只由 runtime 输出的回答" not in capsys.readouterr().out


def test_session_runs_real_runtime_and_read_tool_without_network(
    config_dir, model_factory, monkeypatch, capsys
):
    class ToolCapableModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    note = config_dir.parent / "note.md"
    note.write_text("测试文件的完整内容", encoding="utf-8")
    model_factory.return_value = ToolCapableModel(responses=[
        AIMessage(content="我先读取文件。", tool_calls=[{
            "name": "read", "args": {"file": str(note)}, "id": "local-read",
        }]),
        AIMessage(content="最终总结：这是一份测试说明。"),
    ])
    module = import_module("omt.coding_agent.session")
    monkeypatch.setattr(module, "PromptSession", Mock())
    monkeypatch.setattr(module, "_get_user_input", Mock(side_effect=["读取说明", "/exit"]))

    session()

    output = capsys.readouterr().out
    assert "[工具开始] read" in output
    assert "测试文件的完整内容" in output
    assert "[工具完成] read" in output
    assert output.count("最终总结：这是一份测试说明。") == 1
    assert "test-only-key" not in output
    model_factory.assert_called_once()


def test_get_user_input_can_create_its_own_prompt_session(monkeypatch):
    module = import_module("omt.coding_agent.session")
    with create_pipe_input() as pipe:
        terminal = PromptSession[str](input=pipe, output=DummyOutput())
        factory = Mock(return_value=terminal)
        monkeypatch.setattr(module, "PromptSession", factory)
        pipe.send_text("独立读取输入\n")

        assert _get_user_input() == "独立读取输入"

    factory.assert_called_once_with()


def test_session_can_continue_after_keyboard_interrupt_during_run(
    config_dir, model_factory, monkeypatch
):
    module = import_module("omt.coding_agent.session")
    runner = Mock()
    runner.run.side_effect = [KeyboardInterrupt(), "下一轮回答"]
    monkeypatch.setattr(module, "CreateAgentSession", Mock(return_value=runner))
    monkeypatch.setattr(module, "PromptSession", Mock())
    monkeypatch.setattr(module, "_get_user_input", Mock(side_effect=["取消本轮", "下一轮", "/exit"]))

    session()

    assert [call.args for call in runner.run.call_args_list] == [("取消本轮",), ("下一轮",)]


def test_session_allows_retry_after_failed_turn(config_dir, model_factory, monkeypatch):
    module = import_module("omt.coding_agent.session")
    runner = Mock()
    runner.run.side_effect = [RuntimeError("本地测试失败"), "第二次成功"]
    monkeypatch.setattr(module, "CreateAgentSession", Mock(return_value=runner))
    monkeypatch.setattr(module, "PromptSession", Mock())
    monkeypatch.setattr(module, "_get_user_input", Mock(side_effect=["第一轮", "重试", "/exit"]))

    session()

    assert [call.args for call in runner.run.call_args_list] == [("第一轮",), ("重试",)]
