"""/login 自动保存、认证复用和运行中切换；所有模型、目录及终端均离线隔离。"""

import asyncio
import json
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
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic import Field, HttpUrl, SecretStr

from omt.agent.agent import Agent
from omt.ai import models_manager
from omt.ai.model import Model
from omt.ai.models.deepseek import DEEPSEEK_BASE_URL
from omt.cli.app import run_cli
from omt.cli.login import login
from omt.cli.settings import CliSettings
from omt.cli.terminal import InputValidator
from omt.core import bootstrap, model_configuration, settings
from omt.core.conversation import Conversation
from omt.core.session_manager import SessionError
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


def seed_login() -> None:
    _ = save_model_login(ModelLogin(provider="deepseek", model_name="flash", api_key=SecretStr("fake-private-key")),
                         model_names=("flash", "pro"))


@pytest.fixture(autouse=True)
def catalog(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    for key in ("defaultProvider", "defaultModel", "defaultThinkingLevel"):
        monkeypatch.delenv(key, raising=False)
    discover = AsyncMock(return_value=["flash", "pro"])
    monkeypatch.setattr(model_configuration, "discover_models", discover)
    return discover


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> Mock:
    model = RecordingModel(responses=[AIMessage(content="第一轮回答"), AIMessage(content="第二轮回答")])
    factory = Mock(return_value=model)
    monkeypatch.setattr(import_module("omt.ai.models.deepseek"), "init_chat_model", factory)
    return factory


def test_missing_configuration_enters_cli_and_only_prompts_login_without_writes(
    sdk: Mock, capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(run_cli(terminal=terminal_for(["普通请求", "/resume", "/thinking", "/exit"])))
    output = capsys.readouterr().out
    assert "请输入 /login" in output and "尚无可用模型" in output
    assert not settings.root.exists() and not CliSettings().db_path.exists()
    sdk.assert_not_called()


def test_first_login_only_asks_provider_key_and_model_and_saves_full_catalog(
    sdk: Mock, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    terminal = terminal_for([" /LoGiN ", "deepseek", "fake-private-key", "flash", "正常问题", "/exit"])
    asyncio.run(run_cli(terminal=terminal))
    assert settings.load_settings().defaultModel == "flash"
    assert sdk.call_count == 1
    assert sdk.call_args.kwargs["model"] == "flash"
    assert sdk.call_args.kwargs["base_url"] == str(DEEPSEEK_BASE_URL)
    assert sdk.call_args.kwargs["api_key"] == SecretStr("fake-private-key")
    assert [entry.id for entry in settings.load_model_store()["deepseek"].models] == ["flash", "pro"]
    records = asyncio.run(SessionStore(CliSettings().db_path).list_sessions(tmp_path))
    assert len(records) == 1 and records[0].title == "正常问题"
    output = capsys.readouterr().out
    assert "1. deepseek（未登录）" in output and "第一轮回答" in output
    assert "fake-private-key" not in output
    prompts = [call.args[0] for call in terminal.prompt_async.call_args_list]
    assert not any("base_url" in prompt or "确认" in prompt or "y/N" in prompt for prompt in prompts)


def test_logged_in_provider_only_asks_model_and_keeps_credentials_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    seed_login()
    auth = settings.root / "agent" / "auth.json"
    before = auth.read_bytes()
    terminal = terminal_for(["1", "2"])
    result = asyncio.run(login(terminal))
    assert result is not None and result.model_name == "pro"
    assert terminal.prompt_async.await_count == 2
    assert auth.read_bytes() == before
    assert settings.load_settings().defaultModel == "pro"
    assert "deepseek（已登录）" in capsys.readouterr().out


def test_login_switches_running_model_and_preserves_one_session_history(
    cli_db: Path, sdk: Mock, tmp_path: Path,
) -> None:
    seed_login()
    old = RecordingModel(responses=[AIMessage(content="Flash 回答")])
    new = RecordingModel(responses=[AIMessage(content="Pro 回答")])
    sdk.side_effect = [old, new]
    terminal = terminal_for(["第一条", "/login", "deepseek", "pro", "第二条", "/exit"])
    asyncio.run(run_cli(terminal=terminal))
    assert [call.kwargs["model"] for call in sdk.call_args_list] == ["flash", "pro"]
    assert settings.load_settings().defaultModel == "pro"
    assert len(asyncio.run(SessionStore(cli_db).list_sessions(tmp_path))) == 1
    assert len(old.seen) == len(new.seen) == 1
    assert [message.text for message in new.seen[0] if message.type != "system"] == ["第一条", "Flash 回答", "第二条"]


def test_login_command_itself_never_registers_empty_session(sdk: Mock, tmp_path: Path) -> None:
    asyncio.run(run_cli(terminal=terminal_for(["/login", "deepseek", "fake-private-key", "1", "/exit"])))
    assert asyncio.run(SessionStore(CliSettings().db_path).list_sessions(tmp_path)) == []
    model: RecordingModel = sdk.return_value
    assert not model.seen


def test_failed_refresh_reuses_cached_models_without_new_key(catalog: AsyncMock, capsys: pytest.CaptureFixture[str]) -> None:
    seed_login()
    catalog.side_effect = ValueError("fake-private-key")
    terminal = terminal_for(["deepseek", "pro"])
    assert asyncio.run(login(terminal)) is not None
    assert terminal.prompt_async.await_count == 2
    output = capsys.readouterr().out
    assert "使用已缓存" in output and "fake-private-key" not in output
    assert settings.load_settings().defaultModel == "pro"


def test_first_fetch_failure_does_not_save_or_offer_nonexistent_models(
    catalog: AsyncMock, capsys: pytest.CaptureFixture[str],
) -> None:
    catalog.side_effect = ValueError("fake-private-key")
    terminal = terminal_for(["deepseek", "fake-private-key"])
    assert asyncio.run(login(terminal)) is None
    assert not settings.root.exists()
    assert terminal.prompt_async.await_count == 2
    assert "fake-private-key" not in capsys.readouterr().out


@pytest.mark.parametrize("ending", [EOFError(), KeyboardInterrupt(), asyncio.CancelledError()])
def test_cancel_before_model_choice_has_no_file_side_effects(ending: BaseException) -> None:
    terminal = terminal_for(["deepseek", "fake-private-key", ending])
    if isinstance(ending, (KeyboardInterrupt, asyncio.CancelledError)):
        with pytest.raises(type(ending)):
            _ = asyncio.run(login(terminal))
    else:
        assert asyncio.run(login(terminal)) is None
    assert not settings.root.exists() and not CliSettings().db_path.exists()


def test_invalid_provider_and_model_choices_retry_without_extra_fields() -> None:
    terminal = terminal_for(["unknown", "0", "deepseek", "fake-private-key", "missing", "99", "pro"])
    result = asyncio.run(login(terminal))
    assert result is not None and result.model_name == "pro"
    assert settings.load_settings().defaultModel == "pro"


def test_new_registered_provider_uses_same_simple_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(models_manager.models, "second", models_manager.ModelProvider(
        initialize=Mock(side_effect=AssertionError("不初始化聊天模型")), list_models=Mock(return_value=[]),
        base_url=HttpUrl("https://second.test"),
    ))
    result = asyncio.run(login(terminal_for(["second", "fake-key", "pro"])))
    assert result is not None and result.provider == "second"
    assert settings.load_settings().defaultProvider == "second"
    assert all(str(entry.baseUrl) == "https://second.test/" for entry in settings.load_model_store()["second"].models)


def test_login_with_arguments_is_not_sent_to_model_or_saved(sdk: Mock) -> None:
    asyncio.run(run_cli(terminal=terminal_for(["/login bad-argument", "/exit"])))
    sdk.assert_not_called()
    assert not settings.root.exists()


@pytest.mark.parametrize("cancel", [False, True])
def test_real_terminal_key_history_and_validator_are_restored(cancel: bool) -> None:
    with create_pipe_input() as pipe:
        history = InMemoryHistory()
        validator = InputValidator()
        terminal = PromptSession[str](input=pipe, output=DummyOutput(), history=history, validator=validator)
        pipe.send_text("deepseek\nfake-private-key\n" + ("\x04" if cancel else "2\n"))

        async def execute() -> model_configuration.LoginResult | None:
            return await asyncio.wait_for(login(terminal), 5)

        result = asyncio.run(execute())
        assert (result is None) is cancel
        assert "fake-private-key" not in history.get_strings()
        assert "fake-private-key" not in terminal.default_buffer.text
        assert terminal.default_buffer.history is history
        assert terminal.is_password is False and terminal.validator is validator
    if cancel:
        assert not settings.root.exists()


@pytest.mark.usefixtures("cli_db")
def test_model_switch_uses_new_thinking_map_and_keeps_failed_session_guard(
    sdk: Mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_login()
    path = settings.root / "agent" / "model-store.json"
    _ = path.write_text(json.dumps({"deepseek": {"models": [
        {"id": "flash", "baseUrl": str(DEEPSEEK_BASE_URL)},
        {"id": "pro", "baseUrl": str(DEEPSEEK_BASE_URL), "thinkingLevelMap": {"high": "max"}},
    ]}}))

    async def fail(_self: Conversation, _message: str) -> str:
        raise RuntimeError("本地流失败")

    async def execute() -> None:
        async with bootstrap.open_session_runtime(CliSettings()) as runtime:
            previous = runtime.manager
            record = await previous.create_session()
            _ = await previous.run(record.thread_id, "完成一轮")
            monkeypatch.setattr(Conversation, "arun", fail)
            with pytest.raises(SessionError):
                _ = await previous.run(record.thread_id, "失败一轮")
            await runtime.set_model("deepseek", "pro")
            assert runtime.thinking.model == "pro"
            assert runtime.thinking.available_levels == ("none", "high")
            with pytest.raises(SessionError) as incomplete:
                _ = await runtime.manager.get_history(record.thread_id)
            assert incomplete.value.code == "session_incomplete"
            with pytest.raises(SessionError) as closed:
                _ = await previous.list_sessions()
            assert closed.value.code == "manager_closed"
            await runtime.set_thinking_level("high")
            assert runtime.thinking.reasoning_effort == "max"
        assert sdk.call_args.kwargs["reasoning_effort"] == "max"
    asyncio.run(execute())


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.usefixtures("cli_db", "sdk")
def test_failed_or_cancelled_model_switch_keeps_runtime_and_allows_retry(
    monkeypatch: pytest.MonkeyPatch, cancel: bool,
) -> None:
    seed_login()
    original = bootstrap.build_agent

    async def execute() -> None:
        started = asyncio.Event()
        calls = 0

        async def build(
            model: Model, *, checkpointer: BaseCheckpointSaver[str] | None = None,
            extra_tools: Sequence[BaseTool] = (),
        ) -> Agent:
            nonlocal calls
            calls += 1
            if calls == 2:
                started.set()
                if cancel:
                    _ = await asyncio.Event().wait()
                raise ValueError("本地构建失败")
            return await original(model, checkpointer=checkpointer, extra_tools=extra_tools)

        monkeypatch.setattr(bootstrap, "build_agent", build)
        async with bootstrap.open_session_runtime(CliSettings()) as runtime:
            previous = runtime.manager
            task = asyncio.create_task(runtime.set_model("deepseek", "pro"))
            try:
                if cancel:
                    _ = await asyncio.wait_for(started.wait(), 5)
                    with pytest.raises(SessionError) as busy:
                        await runtime.set_model("deepseek", "flash")
                    assert busy.value.code == "manager_busy"
                    _ = task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    with pytest.raises(ValueError):
                        await task
                assert runtime.manager is previous and runtime.thinking.model == "flash"
                await runtime.set_model("deepseek", "pro")
                assert runtime.manager is not previous and runtime.thinking.model == "pro"
            finally:
                _ = task.cancel()
                _ = await asyncio.gather(task, return_exceptions=True)
    asyncio.run(execute())


@pytest.mark.usefixtures("cli_db")
def test_switch_failure_keeps_running_model_but_records_saved_default(
    sdk: Mock, capsys: pytest.CaptureFixture[str],
) -> None:
    seed_login()
    old = RecordingModel(responses=[AIMessage(content="仍用原模型")])
    sdk.side_effect = [old, RuntimeError("fake-private-key")]
    asyncio.run(run_cli(terminal=terminal_for(["/login", "deepseek", "pro", "继续", "/exit"])))
    assert settings.load_settings().defaultModel == "pro"
    assert len(old.seen) == 1
    output = capsys.readouterr().out
    assert "模型切换失败" in output and "仍用原模型" in output
    assert "fake-private-key" not in output


def test_initialization_failure_after_login_keeps_cli_available(sdk: Mock, capsys: pytest.CaptureFixture[str]) -> None:
    sdk.side_effect = RuntimeError("fake-private-key")
    asyncio.run(run_cli(terminal=terminal_for(["/login", "deepseek", "fake-private-key", "flash", "不应执行", "/exit"])))
    assert settings.load_settings().defaultModel == "flash"
    output = capsys.readouterr().out
    assert "模型初始化失败" in output and "尚无可用模型" in output
    assert "fake-private-key" not in output
