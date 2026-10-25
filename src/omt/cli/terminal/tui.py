"""Textual 交互入口；只编排 core 公开能力，不直接读取或写入用户配置。"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from typing import final, override

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.theme import Theme
from textual.widgets import LoadingIndicator, OptionList, Static, TextArea
from textual.worker import Worker, WorkerCancelled, WorkerFailed

from omt.cli.settings import CliSettings
from omt.core import model_configuration
from omt.core.bootstrap import SessionRuntime, open_session_runtime
from omt.core.events import RunEvent
from omt.core.model_configuration import LoginResult
from omt.core.session_manager import SessionError, SessionManager, validate_message
from omt.core.store import SessionRecord, initialize_session_database

from .completion import COMMANDS, CommandMenu
from .plain import format_output
from .screens import ChoiceScreen, LoginScreen
from .widgets import ChatMessage, Composer, ToolOutput

RuntimeFactory = Callable[[CliSettings], AbstractAsyncContextManager[SessionRuntime | SessionManager]]


@final
class StreamEvent(Message):
    def __init__(self, event: RunEvent) -> None:
        super().__init__()
        self.event = event


@final
class OmtApp(App[None]):
    """可注入运行环境的 TUI；默认只在本地配置就绪或登录成功后加载模型。

    options 是 CLI 启动配置；省略时读取 OMT_CLI_ 环境设置，不代替模型三份 JSON。
    runtime_factory 在一次 App 生命周期内持有 Runtime/固定 Manager，退出时关闭；
    自定义工厂绕过模型配置检查，便于嵌入和离线测试。业务操作串行，Esc 取消当前
    操作但不退出；Ctrl-Q/Ctrl-C 退出并等待清理。取消不回滚工具或检查点副作用。
    """

    TITLE = "OMT"
    SUB_TITLE = "终端助手"
    BINDINGS = [
        Binding("f2", "login", "模型"), Binding("f3", "sessions", "会话"),
        Binding("f4", "thinking", "推理"), Binding("ctrl+n", "new_chat", "新对话", priority=True),
        Binding("escape", "stop", "停止"), Binding("ctrl+q", "close", "退出", priority=True),
        Binding("ctrl+c", "close", show=False, priority=True),
    ]
    CSS_PATH = "tui.tcss"

    def __init__(self, options: CliSettings | None = None, *, runtime_factory: RuntimeFactory | None = None) -> None:
        super().__init__()
        self.register_theme(Theme(name="omt", primary="#d6a079", secondary="#a8adb5",
                                  accent="#d6a079", foreground="#deded9", background="#191a1d",
                                  surface="#212226", panel="#37393f", success="#a3b990",
                                  error="#d98b87", warning="#d5b47b"))
        self.theme = "omt"
        self.options = options or CliSettings()
        self.factory = runtime_factory or open_session_runtime
        self.active: SessionRuntime | SessionManager | None = None
        self.current: SessionRecord | None = None
        self.busy = False
        # 不覆盖 Textual MessagePump._closing；否则退出时消息泵会卡住。
        self._shutting_down = False
        self._stack = AsyncExitStack()
        self._operation: Worker[None] | None = None
        self._assistant: ChatMessage | None = None
        # 实时 RunEvent 没有调用 ID，只能按名称和开始顺序归组；历史另按持久化 ID 精确匹配。
        self._tools: dict[str, list[ToolOutput]] = {}
        self.transcript = VerticalScroll(id="transcript")
        self.command_menu = CommandMenu()
        self.composer = Composer(self.command_menu)
        self.model_status = Static("正在检查配置…", id="model-status", markup=False)
        self.key_hint = Static("", id="key-hint", markup=False)
        self.activity = Horizontal(LoadingIndicator(id="spinner"),
                                   Static("正在处理 · Esc 停止", id="activity-label"), id="activity")

    @override
    def compose(self) -> ComposeResult:
        with Vertical(id="shell"):
            with self.transcript:
                yield Static(Text.assemble(
                    ("OMT", "bold #d6a079"), ("  coding agent\n", "dim"),
                    (f"{self.options.workspace_path}\n\n", "dim"),
                    ("描述任务，直接开始。输入 / 查看指令，↑↓ 选择，Tab 补全。", "dim"),
                ), id="welcome")
            with Vertical(id="dock"):
                yield self.activity
                yield self.command_menu
                with Horizontal(id="compose-bar"):
                    yield Static("❯", id="prompt")
                    yield self.composer
                with Horizontal(id="status-bar"):
                    yield self.model_status
                    yield self.key_hint

    def on_mount(self) -> None:
        _ = self.start_operation(self._boot)

    async def _boot(self) -> None:
        if self.factory is open_session_runtime:
            status = model_configuration.check_model_configuration()
            if not status.ready:
                await self.say("连接模型", status.message + "\n输入 /login 或按 F2，首次连接只需 API Key。")
                return
        await self._open_runtime()

    async def _open_runtime(self) -> None:
        self.active = await self._stack.enter_async_context(self.factory(self.options))

    async def on_unmount(self) -> None:
        self._shutting_down = True
        if self._operation is not None and not self._operation.is_finished:
            self._operation.cancel()
            try:
                await self._operation.wait()
            except (WorkerCancelled, WorkerFailed):
                pass  # Worker 的取消/失败不阻止已打开资源退出；错误已在操作内安全展示。
        await self._stack.aclose()

    def manager(self) -> SessionManager:
        if self.active is None:
            raise SessionError("model_unavailable", "尚未连接模型，请按 F2。")
        return self.active.manager if isinstance(self.active, SessionRuntime) else self.active

    def update_status(self) -> None:
        session = " ".join((self.current.title or "对话").split()) if self.current else "新对话"
        if isinstance(self.active, SessionRuntime):
            state = self.active.thinking
            model_label = state.model if self.size.width < 76 else f"{state.provider}/{state.model}"
            text = f"{model_label} · {state.level}"
            if state.level != state.reasoning_effort:
                text += f" → {state.reasoning_effort}"
        else:
            text = "固定模型" if self.active is not None else "未连接 · /login"
        status = f"{text} · {session}" if self.size.width >= 76 else text
        self.model_status.update(Text(status, no_wrap=True, overflow="ellipsis"))
        self.activity.display = self.busy
        self.update_hint()

    def start_operation(self, operation: Callable[[], Awaitable[None]]) -> bool:
        if self.busy or self._shutting_down:
            self.notify("请等待当前操作，或按 Esc 停止。", severity="warning")
            return False
        self.busy = True
        self.update_status()
        self._operation = self.run_worker(self._perform(operation), group="operation", exit_on_error=False)
        return True

    async def _perform(self, operation: Callable[[], Awaitable[None]]) -> None:
        try:
            await operation()
        except asyncio.CancelledError:
            if not self._shutting_down and self.transcript.is_mounted:
                await self.say("已停止", "操作已取消，已有工具或检查点副作用不会回滚；失败会话请新建或人工检查后恢复。")
            raise
        except SessionError as exc:
            await self.say("操作未完成", str(exc))
        except Exception:
            await self.say("操作未完成", "请检查模型配置、网络或会话存储后重试。未显示可能包含凭据的底层错误。")
        finally:
            self.busy = False
            if not self._shutting_down and self.composer.is_mounted:
                self.update_status()
                if len(self.screen_stack) == 1:
                    _ = self.composer.focus()

    async def say(self, role: str, text: str, *, markdown: bool = False) -> None:
        follow = self.transcript.is_vertical_scroll_end
        await self.transcript.mount(ChatMessage(role, text, markdown=markdown))
        if follow:
            self.transcript.scroll_end(animate=False)

    @on(Composer.Submitted)
    def submitted(self, event: Composer.Submitted) -> None:
        self.submit(event.text)

    @on(TextArea.Changed, "#composer")
    def composer_changed(self) -> None:
        self.composer.fit_height()
        levels = self.active.thinking.available_levels if isinstance(self.active, SessionRuntime) else ()
        self.command_menu.update_query(self.composer.text, levels)
        self.update_hint()

    @on(OptionList.OptionSelected, "#command-menu")
    def command_selected(self, event: OptionList.OptionSelected) -> None:
        _ = event.stop()
        if event.option_id is not None:
            self.submit(event.option_id)

    @on(CommandMenu.Dismissed)
    def completion_dismissed(self) -> None:
        self.update_hint()

    def update_hint(self) -> None:
        if self.command_menu.display:
            text = "Tab 补全 · Enter 执行"
        elif self.size.width < 76:
            text = "/ 指令"
        else:
            text = "/ 指令 · Enter 发送 · Ctrl-J 换行"
        self.key_hint.update(Text(text, no_wrap=True))

    def on_resize(self) -> None:
        if self.composer.is_mounted:
            self.update_status()

    def submit(self, text: str) -> None:
        command = text.strip().split()
        if command:
            name = command[0].lower()
            if name in {"/exit", "/quit"}:
                if len(command) != 1:
                    self.notify("退出命令不接受参数。", severity="warning")
                    return
                self.action_close()
                return
            if name in {"/login", "/model", "/resume", "/thinking", "/new", "/help"}:
                if self.busy:
                    self.notify("请等待当前操作，或按 Esc 停止。", severity="warning")
                    return
                if len(command) > (2 if name == "/thinking" else 1):
                    self.notify("请选择工具栏操作；/thinking 可附一个等级。", severity="warning")
                    return
                _ = self.composer.clear()
                if name == "/thinking" and len(command) == 2:
                    _ = self.start_operation(lambda: self.change_thinking(command[1]))
                else:
                    {"/login": self.action_login, "/model": self.action_login, "/resume": self.action_sessions,
                     "/thinking": self.action_thinking, "/new": self.action_new_chat,
                     "/help": self.action_help}[name]()
                return
            if name.startswith("/"):
                self.notify("未知命令，请输入 /help 查看操作。", severity="warning")
                return
        try:
            _ = validate_message(text)
        except ValueError:
            self.notify("请输入 1～32000 字符的非空内容，不能包含 NUL。", severity="warning")
            return
        if self.active is None:
            self.notify("尚未连接模型，请按 F2 或点“模型”。", severity="warning")
            return
        if self.start_operation(lambda: self.send_message(text)):
            self.composer.remember(text)
            _ = self.composer.clear()

    async def send_message(self, text: str) -> None:
        manager = self.manager()
        if self.current is None:
            self.current = await manager.create_session(title=text[:10])
        self._assistant = None
        self._tools.clear()
        await self.say("你", text)
        result = await manager.run(self.current.thread_id, text, event_handler=self.receive_event)
        if "session_timestamp_update_failed" in result.warnings:
            await self.say("提示", "回答已完成，但更新时间保存失败，请勿因此重复执行。")

    def receive_event(self, event: RunEvent) -> None:
        _ = self.post_message(StreamEvent(event))

    async def on_stream_event(self, message: StreamEvent) -> None:
        event = message.event
        follow = self.transcript.is_vertical_scroll_end
        if event.kind in {"assistant.started", "assistant.delta"}:
            if event.kind == "assistant.started" or self._assistant is None:
                self._assistant = ChatMessage("助手", "", markdown=True)
                await self.transcript.mount(self._assistant)
            if event.kind == "assistant.delta":
                await self._assistant.append_text(event.text or "")
        elif event.kind == "tool.started":
            name = event.tool_name or "工具"
            tool = ToolOutput(name, format_output(event.data))
            self._tools.setdefault(name, []).append(tool)
            await self.transcript.mount(tool)
        elif event.kind in {"tool.delta", "tool.completed", "tool.failed"}:
            pending = self._tools.get(event.tool_name or "工具", [])
            if pending:
                pending[0].add_output(format_output(event.data) if event.data is not None else event.text or "",
                                      finished=event.kind != "tool.delta", failed=event.kind == "tool.failed")
                if event.kind != "tool.delta":
                    _ = pending.pop(0)
        elif event.kind in {"skills.diagnostic", "run.failed"}:
            await self.say("提示", event.text or "本轮未完成。")
        if follow:
            self.transcript.scroll_end(animate=False)

    def action_login(self) -> None:
        if not self.busy and len(self.screen_stack) == 1:
            self.command_menu.dismiss()
            _ = self.push_screen(LoginScreen(), self.login_finished)

    def login_finished(self, result: LoginResult | None) -> None:
        if result is not None:
            _ = self.start_operation(lambda: self.apply_login(result))

    async def apply_login(self, result: LoginResult) -> None:
        if isinstance(self.active, SessionRuntime):
            try:
                await self.active.set_model(result.provider, result.model_name)
            except Exception:
                await self.say("切换失败", "默认选择已保存，当前仍使用原模型；请检查配置后重试。")
                return
        elif self.active is None:
            initialize_session_database(self.options.db_path)
            await self._open_runtime()
        else:
            await self.say("已保存", "当前是固定 Manager，不能在运行中切换模型。")
            return
        await self.say("模型已连接", f"{result.provider} / {result.model_name}，默认选择已保存，会话历史保留。")

    def action_sessions(self) -> None:
        if len(self.screen_stack) == 1:
            self.command_menu.dismiss()
            _ = self.start_operation(self.show_sessions)

    async def show_sessions(self) -> None:
        records = await self.manager().list_sessions()
        choices = [(f"{record.title or '未命名'} · {record.updated_at}", record.thread_id) for record in records]
        _ = self.push_screen(ChoiceScreen("恢复当前工作目录的会话", choices), self.session_selected)

    def session_selected(self, session_id: str | None) -> None:
        if session_id is not None:
            _ = self.start_operation(lambda: self.restore_session(session_id))

    async def restore_session(self, session_id: str) -> None:
        opened = await self.manager().open_session(session_id)
        self.current = opened.record
        await self.transcript.remove_children()
        await self.show_history(opened.messages)
        if opened.warnings:
            await self.say("提示", "会话已恢复，但更新时间保存失败。")

    async def show_history(self, messages: Sequence[AnyMessage]) -> None:
        """按保存的调用 ID 配对工具参数/结果，不能因同名工具完成顺序不同而错配。"""
        pending: dict[str, ToolOutput] = {}
        for message in messages:
            if isinstance(message, SystemMessage):
                continue
            if isinstance(message, ToolMessage):
                tool = pending.pop(message.tool_call_id, None)
                if tool is None:
                    tool = ToolOutput(message.name or "工具", "历史中未提供调用参数")
                    await self.transcript.mount(tool)
                tool.add_output(message.text, finished=True, failed=message.status == "error")
            elif isinstance(message, (AIMessage, HumanMessage)):
                if message.text:
                    await self.say("助手" if isinstance(message, AIMessage) else "你", message.text,
                                   markdown=isinstance(message, AIMessage))
                if isinstance(message, AIMessage):
                    for call in message.tool_calls:
                        tool = ToolOutput(call["name"], format_output(call["args"]))
                        if call["id"] is not None:
                            pending[call["id"]] = tool
                        await self.transcript.mount(tool)

    def action_thinking(self) -> None:
        if not self.busy and len(self.screen_stack) == 1:
            if isinstance(self.active, SessionRuntime):
                self.command_menu.dismiss()
                _ = self.push_screen(ChoiceScreen("推理等级（仅当前进程）", [(level, level) for level in self.active.thinking.available_levels]),
                                 self.thinking_selected)
            else:
                self.notify("请先连接可切换的模型。", severity="warning")

    def thinking_selected(self, level: str | None) -> None:
        if level is not None:
            _ = self.start_operation(lambda: self.change_thinking(level))

    async def change_thinking(self, level: str) -> None:
        if not isinstance(self.active, SessionRuntime):
            raise SessionError("model_unavailable", "当前未提供可切换的模型。")
        await self.active.set_thinking_level(level)

    def action_new_chat(self) -> None:
        if not self.busy and len(self.screen_stack) == 1:
            self.current = None
            _ = self.start_operation(self.clear_chat)

    async def clear_chat(self) -> None:
        await self.transcript.remove_children()
        await self.say("新对话", "输入第一条消息后才登记会话；旧会话仍可按 F3 恢复。")

    def action_help(self) -> None:
        _ = self.start_operation(self.say_help)

    async def say_help(self) -> None:
        commands = "\n".join(f"{hint.value} — {hint.description}" for hint in COMMANDS)
        help_text = ("Enter 发送，Ctrl-J 换行；输入 / 显示指令，↑↓ 选择，Tab 补全，Enter 执行。\n"
                     + "↑↓ 在首/末行回看已发送输入，保留未发送草稿；Esc 收起提示或停止，Ctrl-Q 退出。\n\n"
                     + commands)
        await self.say("操作指南", help_text)

    def action_stop(self) -> None:
        if self._operation is not None and not self._operation.is_finished:
            self._operation.cancel()

    def action_close(self) -> None:
        self._shutting_down = True
        self.exit()
