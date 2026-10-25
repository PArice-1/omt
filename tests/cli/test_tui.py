"""经 Textual Pilot 操作真实界面；用户目录、模型、工具和数据库均隔离。"""

import asyncio
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from importlib import import_module
from pathlib import Path
from typing import Self, override
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from textual.pilot import Pilot
from textual.widgets import Button, Input, Select, Static

from omt.agent.agent import Agent
from omt.cli.app import main
from omt.cli.settings import CliSettings
from omt.cli.terminal.screens import ChoiceScreen, LoginScreen
from omt.cli.terminal.tui import OmtApp, RuntimeFactory
from omt.cli.terminal.widgets import ChatMessage, ToolOutput
from omt.core import model_configuration, settings
from omt.core.events import RunEvent
from omt.core.session_manager import SessionManager
from omt.core.store import SessionStore


class LocalModel(FakeMessagesListChatModel):
    @override
    def bind_tools(self, tools: object, **kwargs: object) -> Self:
        return self


def factory_for(
    model: BaseChatModel, *, tools: Sequence[BaseTool] = (), closed: asyncio.Event | None = None,
) -> RuntimeFactory:
    """使用真实 Manager/Saver；closed 用于确认退出清理已完成，而非仅退出画面。"""
    @asynccontextmanager
    async def factory(options: CliSettings) -> AsyncGenerator[SessionManager]:
        async with AsyncSqliteSaver.from_conn_string(str(options.db_path)) as saver:
            manager = SessionManager(Agent(model, tools=list(tools), checkpointer=saver), SessionStore(options.db_path),
                                     workspace_path=options.workspace_path, system_prompt="不可展示的系统规则", skill_roots=[])
            try:
                yield manager
            finally:
                await manager.aclose()
        if closed is not None:
            closed.set()
    return factory


async def until(pilot: Pilot[None], condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(8):
        while not condition():
            await pilot.pause(0.01)
    await pilot.pause()


async def idle(pilot: Pilot[None], app: OmtApp) -> None:
    await pilot.pause()
    await until(pilot, lambda: not app.busy)


def test_missing_configuration_keyboard_help_and_cancel_leave_no_files() -> None:
    async def execute() -> None:
        app = OmtApp()
        async with app.run_test(size=(90, 30)) as pilot:
            await idle(pilot, app)
            assert app.active is None and not settings.root.exists()
            original_messages = list(app.query(ChatMessage))
            await pilot.press("f2")
            assert isinstance(app.screen, LoginScreen)
            await pilot.press("f2", "f3", "f4", "ctrl+n")
            await idle(pilot, app)
            assert len(app.screen_stack) == 2
            assert list(app.query(ChatMessage)) == original_messages
            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 1 and not settings.root.exists()
            _ = app.composer.insert("/help")
            await pilot.press("enter")
            await idle(pilot, app)
            assert any("Enter 发送" in bubble.text for bubble in app.query(ChatMessage))
            assert not CliSettings().db_path.exists()
            await pilot.press("ctrl+q")
    asyncio.run(execute())


@pytest.mark.parametrize("plain", [False, True])
def test_script_entry_selects_tui_by_default_or_explicit_plain(monkeypatch: pytest.MonkeyPatch, plain: bool) -> None:
    launch = Mock()
    tui = Mock(return_value=Mock(run=launch))
    legacy = AsyncMock()
    monkeypatch.setattr("omt.cli.terminal.tui.OmtApp", tui)
    monkeypatch.setattr("omt.cli.app.run_cli", legacy)
    main(["--plain"] if plain else [])
    if plain:
        legacy.assert_awaited_once()
        tui.assert_not_called()
    else:
        launch.assert_called_once()
        legacy.assert_not_called()


@pytest.mark.parametrize("command", ["/unknown", "/exit extra", "/login inline-secret", "/thinking high extra"])
@pytest.mark.usefixtures("cli_db")
def test_invalid_commands_do_not_run_model_or_exit(command: str, tmp_path: Path) -> None:
    async def execute() -> None:
        model = LocalModel(responses=[AIMessage(content="不应提交命令")])
        app = OmtApp(runtime_factory=factory_for(model))
        async with app.run_test(size=(90, 30)) as pilot:
            await idle(pilot, app)
            _ = app.composer.insert(command)
            await pilot.press("enter")
            await idle(pilot, app)
            assert app.is_running
            assert app.composer.text == command
            assert not any(bubble.role == "你" for bubble in app.query(ChatMessage))
            assert await SessionStore(CliSettings().db_path).list_sessions(tmp_path) == []
            await pilot.press("ctrl+q")
    asyncio.run(execute())


def test_login_thinking_and_model_switch_use_core_without_exposing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("defaultProvider", "defaultModel", "defaultThinkingLevel"):
        monkeypatch.delenv(key, raising=False)
    discover = AsyncMock(return_value=["deepseek-flash", "deepseek-v4-pro"])
    monkeypatch.setattr(model_configuration, "discover_models", discover)
    model = LocalModel(responses=[AIMessage(content="离线回答")])
    sdk = Mock(return_value=model)
    monkeypatch.setattr(import_module("omt.ai.models.deepseek"), "init_chat_model", sdk)

    async def execute() -> None:
        app = OmtApp()
        async with app.run_test(size=(100, 40)) as pilot:
            await idle(pilot, app)
            await pilot.press("f2")
            await pilot.press("enter", "down", "enter")
            await pilot.pause()
            await pilot.press(*"fake-tui-key", "enter")
            await until(pilot, lambda: not app.screen.query_one("#login-model", Select).disabled)
            assert app.screen.query_one("#api-key", Input).value == ""
            assert "fake-tui-key" not in app.export_screenshot()
            await pilot.press("enter", "down", "enter")
            await idle(pilot, app)
            assert settings.load_settings().defaultModel == "deepseek-flash"
            assert app.active is not None
            await pilot.press(*"/thinking h")
            assert app.command_menu.current is not None and app.command_menu.current.value == "/thinking high"
            await pilot.press("tab")
            assert app.composer.text == "/thinking high"
            await pilot.press("enter")
            await idle(pilot, app)
            assert sdk.call_args.kwargs["reasoning_effort"] == "high"
            await pilot.press("f4")
            assert isinstance(app.screen, ChoiceScreen)
            await pilot.press("down", "enter")
            await idle(pilot, app)
            assert sdk.call_args.kwargs["reasoning_effort"] == "high"
            await pilot.press("f2")
            await pilot.press("enter", "down", "enter")
            await until(pilot, lambda: not app.screen.query_one("#login-model", Select).disabled)
            assert not app.screen.query_one("#api-key", Input).display
            await pilot.press("enter", "down", "down", "enter")
            await idle(pilot, app)
            assert settings.load_settings().defaultModel == "deepseek-v4-pro"
            assert sdk.call_args.kwargs["model"] == "deepseek-v4-pro"
            assert sdk.call_args.kwargs["reasoning_effort"] == "high"
            assert "fake-tui-key" not in app.export_screenshot()
            assert await SessionStore(CliSettings().db_path).list_sessions(Path.cwd()) == []
            assert discover.await_count == 2
    asyncio.run(execute())


def test_cancel_directory_fetch_cleans_worker_without_saving(monkeypatch: pytest.MonkeyPatch) -> None:
    async def execute() -> None:
        started, cleaned = asyncio.Event(), asyncio.Event()

        async def discover(_connection: model_configuration.ProviderConnection) -> list[str]:
            started.set()
            try:
                _ = await asyncio.Event().wait()
                return []
            finally:
                cleaned.set()
        monkeypatch.setattr(model_configuration, "discover_models", discover)
        app = OmtApp()
        async with app.run_test(size=(90, 35)) as pilot:
            await idle(pilot, app)
            await pilot.press("f2")
            app.screen.query_one("#provider", Select).value = "deepseek"
            await pilot.pause()
            app.screen.query_one("#api-key", Input).value = "fake-key"
            _ = await pilot.click("#load-models")
            await until(pilot, started.is_set)
            await pilot.press("escape")
            await until(pilot, cleaned.is_set)
            assert not settings.root.exists() and not CliSettings().db_path.exists()
    asyncio.run(execute())


def test_stream_tool_markdown_multiline_and_history_restore(
    cli_db: Path, tmp_path: Path,
) -> None:
    @tool
    def local_note(label: str) -> str:
        """返回测试内创建的说明，不读取用户文件。"""
        return f"本地工具结果：{label}"
    model = LocalModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "local_note", "args": {"label": "离线参数"}, "id": "local-1"}]),
        AIMessage(content="# 总结\n\n这是 **Markdown** 回答。"),
    ])

    async def execute() -> None:
        closed = asyncio.Event()
        app = OmtApp(runtime_factory=factory_for(model, tools=[local_note], closed=closed))
        async with app.run_test(size=(100, 38)) as pilot:
            await idle(pilot, app)
            await pilot.press(*"hello", "ctrl+j", *"world", "enter")
            await idle(pilot, app)
            bubbles = list(app.query(ChatMessage))
            assert [bubble.text for bubble in bubbles if bubble.role == "你"] == ["hello\nworld"]
            assert sum(bubble.text == "# 总结\n\n这是 **Markdown** 回答。" for bubble in bubbles) == 1
            output = app.query_one(ToolOutput)
            assert output.collapsed and "本地工具结果" in output.text
            assert "不可展示的系统规则" not in app.export_screenshot()
            records = await SessionStore(cli_db).list_sessions(tmp_path)
            assert len(records) == 1
            old_id = records[0].thread_id
            await pilot.press("ctrl+n")
            await idle(pilot, app)
            assert app.current is None
            await pilot.press("f3")
            await until(pilot, lambda: len(app.screen_stack) == 2)
            await pilot.press("enter")
            await idle(pilot, app)
            assert app.current is not None and app.current.thread_id == old_id
            assert len(await SessionStore(cli_db).list_sessions(tmp_path)) == 1
            assert any("Markdown" in bubble.text for bubble in app.query(ChatMessage))
            restored = app.query_one(ToolOutput)
            assert '"label": "离线参数"' in restored.text and "本地工具结果" in restored.text
            assert len(app.query(ToolOutput)) == 1
            await pilot.press("ctrl+q")
        assert closed.is_set()
    asyncio.run(execute())


@pytest.mark.parametrize("quit_during_run", [False, True])
@pytest.mark.usefixtures("cli_db")
def test_cancel_or_quit_running_tool_waits_for_cleanup_and_keeps_failure_guard(
    quit_during_run: bool,
) -> None:
    async def execute() -> None:
        started, cleaned, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls: list[str] = []

        @tool
        async def wait_here() -> str:
            """等待界面取消，只处理测试任务。"""
            calls.append("run")
            started.set()
            try:
                _ = await asyncio.Event().wait()
                return "不可达"
            finally:
                cleaned.set()
        model = LocalModel(responses=[AIMessage(content="", tool_calls=[{
            "name": "wait_here", "args": {}, "id": "wait-1",
        }])])
        app = OmtApp(runtime_factory=factory_for(model, tools=[wait_here], closed=closed))
        async with app.run_test(size=(90, 30)) as pilot:
            await idle(pilot, app)
            _ = app.composer.insert("开始等待")
            await pilot.press("enter")
            await until(pilot, started.is_set)
            assert app.activity.display
            _ = app.composer.insert("/ne")
            await pilot.press("enter")
            assert app.busy and app.composer.text == "/ne"
            if not quit_during_run:
                await pilot.press("escape")
                assert not app.command_menu.display and not cleaned.is_set() and app.busy
            await pilot.press("ctrl+q" if quit_during_run else "escape")
            await until(pilot, cleaned.is_set)
            if not quit_during_run:
                await idle(pilot, app)
                _ = app.composer.clear()
                _ = app.composer.insert("不能重跑")
                await pilot.press("enter")
                await idle(pilot, app)
                assert calls == ["run"]
                assert any("上次运行未完成" in bubble.text for bubble in app.query(ChatMessage))
                await pilot.press("ctrl+q")
        assert cleaned.is_set() and closed.is_set()
    asyncio.run(execute())


def test_login_failure_keeps_key_hidden_and_does_not_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def execute() -> None:
        secret = "fake-login-failure-secret"

        async def unavailable(_connection: model_configuration.ProviderConnection) -> list[str]:
            raise RuntimeError(secret)

        monkeypatch.setattr(model_configuration, "discover_models", unavailable)
        app = OmtApp()
        async with app.run_test(size=(90, 32)) as pilot:
            await idle(pilot, app)
            await pilot.press("f2")
            screen = app.screen
            assert isinstance(screen, LoginScreen)
            screen.provider_select.value = "deepseek"
            await pilot.pause()
            screen.key_input.value = secret
            _ = await pilot.click("#load-models")
            await until(pilot, lambda: "未保存" in str(screen.query_one("#login-status", Static).render()))
            assert screen.key_input.password and screen.key_input.value == ""
            assert secret not in app.export_screenshot()
            assert screen.model_select.disabled
            assert not settings.root.exists()
            await pilot.press("escape", "ctrl+q")
    asyncio.run(execute())


def test_small_terminal_login_is_scrollable_and_cancellable() -> None:
    async def execute() -> None:
        app = OmtApp()
        async with app.run_test(size=(90, 32)) as pilot:
            await idle(pilot, app)
            await pilot.resize_terminal(52, 20)
            await pilot.press("f2")
            screen = app.screen
            assert isinstance(screen, LoginScreen)
            screen.provider_select.value = "deepseek"
            await pilot.pause()
            _ = screen.query_one("#login-cancel", Button).focus()
            await pilot.pause()
            cancel = screen.query_one("#login-cancel", Button)
            assert cancel.region.y >= 0 and cancel.region.bottom <= screen.size.height
            await pilot.press("enter")
            await until(pilot, lambda: len(app.screen_stack) == 1)
            assert not settings.root.exists()
            await pilot.press("ctrl+q")
    asyncio.run(execute())


@pytest.mark.usefixtures("cli_db")
def test_sent_input_history_preserves_draft_and_multiline_editing() -> None:
    async def execute() -> None:
        app = OmtApp(runtime_factory=factory_for(LocalModel(responses=[AIMessage(content="回答")])))
        async with app.run_test(size=(90, 30)) as pilot:
            await idle(pilot, app)
            await pilot.press(*"first", "enter")
            await idle(pilot, app)
            await pilot.press(*"second", "enter")
            await idle(pilot, app)
            await pilot.press(*"draft", "up")
            assert app.composer.text == "second"
            await pilot.press("up")
            assert app.composer.text == "first"
            await pilot.press("down", "down")
            assert app.composer.text == "draft"
            app.composer.load_text("line one\nline two")
            app.composer.move_cursor(app.composer.document.end)
            await pilot.press("up")
            assert app.composer.text == "line one\nline two" and app.composer.cursor_location[0] == 0
            await pilot.press("down", "shift+up")
            assert app.composer.selected_text
            assert len(await SessionStore(CliSettings().db_path).list_sessions(Path.cwd())) == 1
            assert len([message for message in app.query(ChatMessage) if message.role == "你"]) == 2
            await pilot.press("ctrl+q")
    asyncio.run(execute())


def test_tool_screen_buffer_limits_parameters_and_stream_output() -> None:
    async def execute() -> None:
        app = OmtApp()
        async with app.run_test(size=(90, 30)) as pilot:
            await idle(pilot, app)
            app.receive_event(RunEvent("tool.started", tool_name="local", data="A" * 20000 + "参数末尾"))
            await pilot.pause()
            output = app.query_one(ToolOutput)
            assert len(output.text) <= 16000 and output.text.endswith("参数末尾")
            app.receive_event(RunEvent("tool.delta", tool_name="local", data="B" * 20000 + "输出末尾"))
            await pilot.pause()
            assert len(output.text) <= 16000 and output.text.endswith("输出末尾")
            app.receive_event(RunEvent("tool.completed", tool_name="local", text="最终结果"))
            await pilot.pause()
            assert len(output.text) <= 16000 and output.text.endswith("最终结果")
            assert output.collapsed and "完成" in str(output.title)
            assert not settings.root.exists()
            await pilot.press("ctrl+q")
    asyncio.run(execute())


def test_history_pairs_parallel_tool_results_by_saved_call_id() -> None:
    async def execute() -> None:
        app = OmtApp()
        async with app.run_test(size=(90, 30)) as pilot:
            await idle(pilot, app)
            await app.show_history([
                AIMessage(content="", tool_calls=[
                    {"name": "same_tool", "args": {"label": "甲参数"}, "id": "call-a"},
                    {"name": "same_tool", "args": {"label": "乙参数"}, "id": "call-b"},
                ]),
                ToolMessage(content="乙结果", tool_call_id="call-b"),
                ToolMessage(content="甲结果", tool_call_id="call-a"),
            ])
            await pilot.pause()
            outputs = list(app.query(ToolOutput))
            assert len(outputs) == 2 and all(output.collapsed for output in outputs)
            assert "甲参数" in outputs[0].text and "甲结果" in outputs[0].text
            assert "乙结果" not in outputs[0].text
            assert "乙参数" in outputs[1].text and "乙结果" in outputs[1].text
            assert outputs[0].tool_name == outputs[1].tool_name == "same_tool"
            assert not settings.root.exists()
            await pilot.press("ctrl+q")
    asyncio.run(execute())


@pytest.mark.parametrize("key", ["ctrl+q", "ctrl+c"])
def test_close_from_login_dialog_leaves_no_files(key: str) -> None:
    async def execute() -> None:
        app = OmtApp()
        async with app.run_test(size=(90, 30)) as pilot:
            await idle(pilot, app)
            await pilot.press("f2")
            assert len(app.screen_stack) == 2
            await pilot.press(key)
            await until(pilot, lambda: not app.is_running)
        assert not settings.root.exists()
    asyncio.run(execute())


def test_new_output_does_not_interrupt_scrollback() -> None:
    async def execute() -> None:
        app = OmtApp()
        async with app.run_test(size=(90, 30)) as pilot:
            await idle(pilot, app)
            await app.say("历史", "历史内容\n" * 40)
            await pilot.pause()
            app.transcript.scroll_home(animate=False)
            await pilot.pause()
            assert not app.transcript.is_vertical_scroll_end
            await app.say("助手", "新输出", markdown=True)
            await pilot.pause()
            assert app.transcript.scroll_y == 0
            assert not settings.root.exists()
            await pilot.press("ctrl+q")
    asyncio.run(execute())
