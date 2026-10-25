"""真实输入区/提示菜单回归；仅使用隔离配置和 Textual Pilot，不初始化远程模型。"""

import asyncio

import pytest
from textual.pilot import Pilot

from omt.cli.terminal.completion import COMMANDS, command_hints
from omt.cli.terminal.screens import LoginScreen
from omt.cli.terminal.tui import OmtApp
from omt.cli.terminal.widgets import ChatMessage, ToolOutput
from omt.core import settings


async def ready(pilot: Pilot[None], app: OmtApp) -> None:
    await pilot.pause()
    async with asyncio.timeout(5):
        while app.busy:
            await pilot.pause()


def test_slash_hints_navigate_complete_without_executing_and_escape_keeps_draft() -> None:
    async def execute() -> None:
        app = OmtApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await ready(pilot, app)
            await pilot.press("/")
            assert app.command_menu.display
            assert [option.id for option in app.command_menu.options] == [hint.value for hint in COMMANDS]
            assert app.focused is app.composer
            await pilot.press("down")
            assert app.command_menu.current is not None and app.command_menu.current.value == "/model"
            await pilot.press("tab")
            assert app.composer.text == "/model" and len(app.screen_stack) == 1
            await pilot.press("escape")
            assert not app.command_menu.display and app.composer.text == "/model"
            assert "Tab 补全" not in str(app.key_hint.render())
            await pilot.press("enter")
            assert isinstance(app.screen, LoginScreen)
            await pilot.press("escape", "ctrl+q")
        assert not settings.root.exists()
    asyncio.run(execute())


@pytest.mark.parametrize("prefix", ["/lo", "/mo", "/LO"])
def test_enter_executes_filtered_command_not_partial_prompt(prefix: str) -> None:
    async def execute() -> None:
        app = OmtApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(pilot, app)
            await pilot.press(*prefix)
            assert app.command_menu.option_count == 1
            await pilot.press("enter")
            assert isinstance(app.screen, LoginScreen)
            assert not app.screen.key_input.display and not app.screen.model_select.display
            assert app.composer.text == ""
            assert not app.command_menu.display
            await pilot.press("escape", "ctrl+q")
        assert not settings.root.exists()
    asyncio.run(execute())


@pytest.mark.parametrize("text", ["普通输入", "/unknown", "/login inline-secret", "/thinking high extra", "/help\n保留多行"])
def test_no_suggestions_for_unknown_arguments_or_multiline_paste(text: str) -> None:
    async def execute() -> None:
        app = OmtApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(pilot, app)
            _ = app.composer.insert(text)
            await pilot.pause()
            assert not app.command_menu.display and app.composer.text == text
            assert not settings.root.exists()
            await pilot.press("ctrl+q")
    asyncio.run(execute())


@pytest.mark.parametrize(("text", "expected"), [
    ("/thi", ["/thinking"]),
    ("/thinking ", ["/thinking none", "/thinking turbo"]),
    ("/thinking T", ["/thinking turbo"]),
    ("/thinking high", []),
    ("/thinking turbo extra", []),
    ("/login key", []),
])
def test_thinking_hints_only_use_runtime_capabilities(text: str, expected: list[str]) -> None:
    assert [hint.value for hint in command_hints(text, ("none", "turbo"))] == expected


@pytest.mark.parametrize(("old", "new"), [("/exit", "/exit extra"), ("/login", "/login inline-secret")])
def test_typeahead_does_not_execute_stale_command(old: str, new: str) -> None:
    async def execute() -> None:
        app = OmtApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(pilot, app)
            app.command_menu.update_query(old)
            app.composer.load_text(new)
            # 不等待 Changed 派发，模拟新文本已到达而菜单仍停留上一帧时立即回车。
            app.composer.action_submit()
            await ready(pilot, app)
            assert app.is_running and len(app.screen_stack) == 1
            assert app.composer.text == new and app.current is None
            assert not settings.root.exists()
            await pilot.press("ctrl+q")
    asyncio.run(execute())


def test_clicking_suggestion_executes_help_without_creating_conversation() -> None:
    async def execute() -> None:
        app = OmtApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await ready(pilot, app)
            await pilot.press(*"/he")
            assert app.command_menu.display
            assert await pilot.click("#command-menu", offset=(5, 1))
            await ready(pilot, app)
            assert any(message.role == "操作指南" for message in app.query(ChatMessage))
            assert app.current is None and app.composer.text == ""
            assert not any(message.role == "你" for message in app.query(ChatMessage))
            assert not settings.root.exists()
            await pilot.press("ctrl+q")
    asyncio.run(execute())


def test_input_autogrows_and_command_menu_fits_small_terminal() -> None:
    async def execute() -> None:
        app = OmtApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(pilot, app)
            assert app.composer.size.height == 1 and app.transcript.size.height >= 18
            app.composer.load_text("wrap" * 14)
            await pilot.pause()
            assert app.composer.size.height == 1
            await pilot.resize_terminal(44, 24)
            await pilot.pause()
            assert app.composer.size.height > 1
            await pilot.resize_terminal(80, 24)
            _ = app.composer.insert("很长的内容" * 50)
            await pilot.pause()
            assert 1 < app.composer.size.height <= 6
            app.composer.load_text("\n".join(str(index) for index in range(20)))
            await pilot.pause()
            assert app.composer.size.height == 6
            app.composer.load_text("/")
            await pilot.resize_terminal(44, 16)
            await pilot.pause()
            assert app.composer.size.height == 1
            assert app.transcript.size.height > 0
            assert app.command_menu.region.y >= 0
            assert app.command_menu.region.bottom <= app.composer.region.y
            assert app.composer.region.bottom < app.screen.size.height
            await pilot.press(*(["down"] * (len(COMMANDS) - 1)))
            assert app.command_menu.current is not None and app.command_menu.current.value == "/quit"
            await pilot.press("escape", "ctrl+q")
        assert not settings.root.exists()
    asyncio.run(execute())


def test_chat_markers_leave_space_for_message_body() -> None:
    async def execute() -> None:
        app = OmtApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(pilot, app)
            await app.say("你", "用户输入")
            await app.say("助手", "回复正文", markdown=True)
            await pilot.pause()
            for message in app.query(ChatMessage):
                if message.role in {"你", "助手"}:
                    marker = message.query_one(".message-marker")
                    assert marker.size.width == 2
                    assert message.body.region.x == marker.region.right
                    assert message.body.size.width >= 70
            await pilot.press("ctrl+q")
    asyncio.run(execute())


def test_tab_keeps_normal_focus_navigation_without_completion_menu() -> None:
    async def execute() -> None:
        app = OmtApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(pilot, app)
            await app.transcript.mount(ToolOutput("测试", "无副作用"))
            _ = app.composer.focus()
            await pilot.press(*"ordinary", "tab")
            assert app.composer.text == "ordinary"
            assert app.focused is not app.composer
            assert not app.command_menu.display
            await pilot.press("ctrl+q")
    asyncio.run(execute())


def test_input_history_is_bounded_ignores_commands_and_preserves_draft() -> None:
    async def execute() -> None:
        app = OmtApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await ready(pilot, app)
            for index in range(60):
                app.composer.remember(str(index))
            app.composer.remember("/login fake-secret")
            app.composer.load_text("未发送草稿")
            for _ in range(60):
                app.composer.action_cursor_up()
            assert app.composer.text == "10"
            for _ in range(60):
                app.composer.action_cursor_down()
            assert app.composer.text == "未发送草稿"
            assert "fake-secret" not in app.export_screenshot()
            assert not settings.root.exists()
            await pilot.press("ctrl+q")
    asyncio.run(execute())
