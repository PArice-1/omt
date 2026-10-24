"""/login 的无模型状态和运行中添加模型回归；不使用真实网络、Key 或用户终端。"""

import asyncio
from collections.abc import Sequence
from importlib import import_module
from pathlib import Path
from typing import Self, override
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult
from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic import Field, HttpUrl, SecretStr

from omt.ai import models_manager
from omt.cli.app import run_cli
from omt.cli.login import login
from omt.cli.settings import CliSettings
from omt.cli.terminal import InputValidator
from omt.core import model_configuration, settings
from omt.core.model_configuration import ModelLogin, save_model_login
from omt.core.store import SessionStore


class RecordingModel(FakeMessagesListChatModel):
    seen: list[list[BaseMessage]] = Field(default_factory=list)

    @override
    def bind_tools(self, tools: Sequence[object], **kwargs: object) -> Self:
        return self

    @override
    def _generate(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None, **kwargs: object,
    ) -> ChatResult:
        self.seen.append(list(messages))
        return super()._generate(messages, stop, run_manager, **kwargs)


def terminal_for(values: Sequence[str | BaseException]) -> Mock:
    return Mock(spec=PromptSession, prompt_async=AsyncMock(side_effect=values),
                default_buffer=Mock(history=Mock()), is_password=False, validator=None)


def login_inputs(model_name: str = "chosen-model") -> list[str]:
    return ["deepseek", "https://example.test/custom/v1", "fake-private-key", "n", model_name, "y"]


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> Mock:
    model = RecordingModel(responses=[AIMessage(content="第一轮回答"), AIMessage(content="第二轮回答")])
    factory = Mock(return_value=model)
    monkeypatch.setattr(import_module("omt.ai.models.deepseek"), "init_chat_model", factory)
    for key in ("defaultProvider", "defaultModel", "defaultThinkingLevel"):
        monkeypatch.delenv(key, raising=False)
    return factory


def test_missing_configuration_enters_cli_and_only_prompts_login_without_writes(
    sdk: Mock, capsys: pytest.CaptureFixture[str],
) -> None:
    terminal = terminal_for(["普通请求", "/resume", "/thinking", "/exit"])
    asyncio.run(run_cli(terminal=terminal))
    output = capsys.readouterr().out
    assert "请输入 /login" in output
    assert "尚无可用模型" in output
    assert not settings.root.exists()
    assert not CliSettings().db_path.exists()
    sdk.assert_not_called()


def test_first_login_passes_all_parameters_to_core_and_can_chat_immediately(
    sdk: Mock, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    terminal = terminal_for([" /LoGiN ", *login_inputs(), "正常问题", "/exit"])
    asyncio.run(run_cli(terminal=terminal))
    assert settings.load_settings().defaultProvider == "deepseek"
    assert settings.load_settings().defaultModel == "chosen-model"
    assert sdk.call_count == 1
    assert sdk.call_args.kwargs["model"] == "chosen-model"
    assert sdk.call_args.kwargs["base_url"] == "https://example.test/custom/v1"
    assert sdk.call_args.kwargs["api_key"] == SecretStr("fake-private-key")
    assert [entry.id for entry in settings.load_model_store()["deepseek"].models] == ["chosen-model"]
    records = asyncio.run(SessionStore(CliSettings().db_path).list_sessions(tmp_path))
    assert len(records) == 1 and records[0].title == "正常问题"
    output = capsys.readouterr().out
    assert "支持的厂商：deepseek" in output
    assert "第一轮回答" in output
    assert "fake-private-key" not in output


def test_login_during_chat_adds_model_without_changing_default_runtime_or_history(
    cli_db: Path, sdk: Mock, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _ = save_model_login(ModelLogin(provider="deepseek", model_name="original-model",
                                    api_key=SecretStr("fake-old-key"), base_url=HttpUrl("https://old.test/v1")))
    selection = settings.root / "agent" / "settings.json"
    before = selection.read_bytes()
    terminal = terminal_for(["第一条", "/login", *login_inputs("additional-model"), "第二条", "/exit"])
    asyncio.run(run_cli(terminal=terminal))
    assert sdk.call_count == 1
    assert sdk.call_args.kwargs["model"] == "original-model"
    assert selection.read_bytes() == before
    assert [entry.id for entry in settings.load_model_store()["deepseek"].models] == ["original-model", "additional-model"]
    assert len(asyncio.run(SessionStore(cli_db).list_sessions(tmp_path))) == 1
    model: RecordingModel = sdk.return_value
    assert [message.text for message in model.seen[-1] if message.type == "human"] == ["第一条", "第二条"]
    assert "本次运行继续使用原模型" in capsys.readouterr().out


def test_login_command_itself_never_registers_empty_session(sdk: Mock, tmp_path: Path) -> None:
    asyncio.run(run_cli(terminal=terminal_for(["/login", *login_inputs(), "/exit"])))
    assert asyncio.run(SessionStore(CliSettings().db_path).list_sessions(tmp_path)) == []
    model: RecordingModel = sdk.return_value
    assert not model.seen


def test_catalog_is_optional_and_routes_through_core(monkeypatch: pytest.MonkeyPatch) -> None:
    discover = AsyncMock(return_value=["first-model", "chosen-model"])
    monkeypatch.setattr(model_configuration, "discover_models", discover)
    terminal = terminal_for(["deepseek", "https://example.test/custom/v1", "fake-private-key",
                             "y", "chosen-model", "y"])
    result = asyncio.run(login(terminal))
    assert result is not None and result.model_name == "chosen-model"
    discover.assert_awaited_once()
    assert settings.load_model_store()["deepseek"].models[0].id == "chosen-model"
    assert len(settings.load_model_store()["deepseek"].models) == 1


def test_failed_catalog_allows_manual_login_without_disclosing_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(model_configuration, "discover_models", AsyncMock(side_effect=ValueError("fake-private-key")))
    result = asyncio.run(login(terminal_for(["deepseek", "https://example.test", "fake-private-key",
                                           "y", "manual-model", "y"])))
    assert result is not None
    assert "fake-private-key" not in capsys.readouterr().out


@pytest.mark.parametrize("ending", ["n", EOFError(), KeyboardInterrupt(), asyncio.CancelledError()])
def test_cancelled_login_has_no_configuration_or_database_side_effects(ending: str | BaseException) -> None:
    terminal = terminal_for([*login_inputs()[:-1], ending])
    if isinstance(ending, (KeyboardInterrupt, asyncio.CancelledError)):
        with pytest.raises(type(ending)):
            _ = asyncio.run(login(terminal))
    else:
        assert asyncio.run(login(terminal)) is None
    assert not settings.root.exists()
    assert not CliSettings().db_path.exists()


def test_unknown_provider_is_rejected_before_asking_for_secret() -> None:
    terminal = terminal_for(["not-registered"])
    assert asyncio.run(login(terminal)) is None
    assert terminal.prompt_async.await_count == 1
    assert not settings.root.exists()


def test_login_lists_and_accepts_new_registered_provider_without_cli_changes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setitem(models_manager.models, "second", models_manager.ModelProvider(
        initialize=Mock(side_effect=AssertionError("不初始化聊天模型")), list_models=Mock(return_value=[]),
    ))
    result = asyncio.run(login(terminal_for(["second", "https://example.test", "fake-key", "n", "new-model", "y"])))
    assert result is not None and result.provider == "second"
    assert "支持的厂商：deepseek、second" in capsys.readouterr().out
    assert settings.load_settings().defaultProvider == "second"


def test_login_with_arguments_is_not_sent_to_model_or_saved(sdk: Mock) -> None:
    asyncio.run(run_cli(terminal=terminal_for(["/login bad-argument", "/exit"])))
    sdk.assert_not_called()
    assert not settings.root.exists()


@pytest.mark.parametrize("cancel", [False, True])
def test_real_terminal_key_is_not_in_history_and_password_mode_is_restored(cancel: bool) -> None:
    with create_pipe_input() as pipe:
        history = InMemoryHistory()
        validator = InputValidator()
        terminal = PromptSession[str](input=pipe, output=DummyOutput(), history=history, validator=validator)
        # 目录查询直接回车选择否，不能继承普通消息的非空限制。
        pipe.send_text("deepseek\nhttps://example.test\nfake-private-key\n" + ("\x04" if cancel else "\nchosen-model\ny\n"))

        async def execute() -> model_configuration.LoginResult | None:
            return await asyncio.wait_for(login(terminal), 5)

        result = asyncio.run(execute())
        assert (result is None) is cancel
        assert "fake-private-key" not in history.get_strings()
        assert "fake-private-key" not in terminal.default_buffer.text
        assert terminal.default_buffer.history is history
        assert terminal.is_password is False
        assert terminal.validator is validator
    if cancel:
        assert not settings.root.exists()


def test_initialization_failure_after_login_keeps_cli_available_and_hides_error(
    sdk: Mock, capsys: pytest.CaptureFixture[str],
) -> None:
    sdk.side_effect = RuntimeError("fake-private-key")
    asyncio.run(run_cli(terminal=terminal_for(["/login", *login_inputs(), "不应执行", "/exit"])))
    assert settings.load_settings().defaultModel == "chosen-model"
    output = capsys.readouterr().out
    assert "模型初始化失败" in output
    assert "尚无可用模型" in output
    assert "fake-private-key" not in output
