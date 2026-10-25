"""TUI 的消息、工具结果和输入展示，不操作会话或模型。"""

from typing import final, override

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Collapsible, Label, Markdown, Static, TextArea

from .completion import CommandMenu


@final
class Composer(TextArea):
    """终端式输入；menu 仅在命令补全时接管方向键/Tab/Esc，普通多行编辑保持原行为。

    输入历史只在 remember 显式接纳普通消息时记录，最多 50 条且仅驻留内存；不记录命令或登录 Key。
    """

    BINDINGS = [
        Binding("enter", "submit", show=False, priority=True),
        Binding("ctrl+j", "newline", show=False, priority=True),
        Binding("tab", "complete", show=False, priority=True),
        Binding("escape", "dismiss_completion", show=False, priority=True),
    ]

    def __init__(self, menu: CommandMenu) -> None:
        super().__init__(id="composer", soft_wrap=True, show_line_numbers=False,
                         highlight_cursor_line=False, placeholder="输入任务，或 / 查看指令")
        self.menu = menu
        self._sent: list[str] = []
        self._history_index = 0
        self._draft = ""

    @override
    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "complete":
            return self.menu.selection_for(self.text) is not None
        if action == "dismiss_completion":
            return self.menu.display
        return super().check_action(action, parameters)

    def fit_height(self) -> None:
        """按软换行后的可见行数扩展到 1～6 行；更长内容留在编辑器内滚动。"""
        self.styles.height = min(6, max(1, self.wrapped_document.height))

    def on_resize(self) -> None:
        # 等编辑器按新宽度完成软换行后再量高度，不能只监听 App 的外层尺寸。
        _ = self.call_after_refresh(self.fit_height)

    @final
    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def action_submit(self) -> None:
        selected = self.menu.selection_for(self.text)
        _ = self.post_message(self.Submitted(selected.value if selected is not None else self.text))

    def action_newline(self) -> None:
        _ = self.insert("\n")

    def action_complete(self) -> None:
        if (selected := self.menu.selection_for(self.text)) is not None:
            self.load_text(selected.insertion)
            self.move_cursor(self.document.end)

    def action_dismiss_completion(self) -> None:
        self.menu.dismiss()

    def remember(self, text: str) -> None:
        if text.lstrip().startswith("/"):
            return
        if not self._sent or self._sent[-1] != text:
            self._sent = [*self._sent, text][-50:]
        self._history_index = len(self._sent)
        self._draft = ""

    def _move_history(self, direction: int) -> None:
        """direction=-1 取更早输入，1 取更新输入；越界时保留当前位置和未发送草稿。"""
        if self._history_index == len(self._sent):
            self._draft = self.text
        self._history_index = min(len(self._sent), max(0, self._history_index + direction))
        text = self._draft if self._history_index == len(self._sent) else self._sent[self._history_index]
        self.load_text(text)
        self.move_cursor(self.document.end)

    @override
    def action_cursor_up(self, select: bool = False) -> None:
        if not select and self.menu.current is not None:
            self.menu.action_cursor_up()
        elif not select and self.cursor_at_first_line and self._sent:
            self._move_history(-1)
        else:
            super().action_cursor_up(select)

    @override
    def action_cursor_down(self, select: bool = False) -> None:
        if not select and self.menu.current is not None:
            self.menu.action_cursor_down()
        elif not select and self.cursor_at_last_line and self._history_index < len(self._sent):
            self._move_history(1)
        else:
            super().action_cursor_down(select)


@final
class ChatMessage(Static):
    """一条消息；text 是正文，不使用 Rich markup 解释用户输入或角色名。"""

    def __init__(self, role: str, text: str, *, markdown: bool = False) -> None:
        super().__init__(classes="chat-message user-message" if role == "你" else "chat-message")
        self.role = role
        self.text = text
        self.body = Markdown(text, open_links=False) if markdown else Static(text, markup=False)

    @override
    def compose(self) -> ComposeResult:
        if self.role in {"你", "助手"}:
            with Horizontal(classes="message-row"):
                yield Label("›" if self.role == "你" else "●", classes="message-marker", markup=False)
                yield self.body
        else:
            yield Label(f"● {self.role}", classes="message-role", markup=False)
            yield self.body

    async def append_text(self, text: str) -> None:
        self.text += text
        if isinstance(self.body, Markdown):
            await self.body.append(text)
        else:
            self.body.update(self.text)


@final
class ToolOutput(Collapsible):
    """按工具调用折叠输出；只限制屏幕展示，不改变真实工具返回值或检查点。"""

    def __init__(self, name: str, parameters: str) -> None:
        self.tool_name = name
        self.text = f"参数：\n{parameters}"[-16000:]
        self.output = Static(self.text, markup=False)
        super().__init__(self.output, title=f"{name} · 执行中", collapsed=True)

    def add_output(self, text: str, *, finished: bool = False, failed: bool = False) -> None:
        self.text = (self.text + "\n" + text)[-16000:]
        self.output.update(self.text)
        if finished:
            self.title = f"{self.tool_name} · {'失败' if failed else '完成'}"
