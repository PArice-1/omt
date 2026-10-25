"""输入区的斜杠命令提示；只提供展示与补全，不执行命令或查询远程模型。"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import final

from rich.text import Text
from textual.message import Message
from textual.widgets import OptionList
from textual.widgets.option_list import Option


@dataclass(frozen=True)
class CommandHint:
    """value 是可提交的完整指令；description 只用于展示，accepts_argument 决定 Tab 后是否留空格。"""

    value: str
    description: str
    accepts_argument: bool = False

    @property
    def insertion(self) -> str:
        return self.value + (" " if self.accepts_argument else "")


COMMANDS = (
    CommandHint("/login", "连接或切换模型 · F2"),
    CommandHint("/model", "选择模型（/login 的别名） · F2"),
    CommandHint("/resume", "恢复当前项目的会话 · F3"),
    CommandHint("/new", "开始新对话 · Ctrl-N"),
    CommandHint("/thinking", "选择推理等级 · F4", accepts_argument=True),
    CommandHint("/help", "查看快捷键与使用帮助"),
    CommandHint("/exit", "清理资源并退出 · Ctrl-Q"),
    CommandHint("/quit", "退出（/exit 的别名）"),
)


def command_hints(text: str, thinking_levels: Sequence[str] = ()) -> tuple[CommandHint, ...]:
    """按当前整段输入筛选；thinking_levels 只接受运行模型的真实等级，不猜测能力。

    多行、普通消息和已有非法参数不补全，避免把粘贴的消息/行内凭据改写成可执行命令。
    返回空元组也可能表示未知前缀，调用方可展示帮助而不自动执行任何操作。
    """
    query = text.lstrip()
    if not query.startswith("/") or "\n" in query:
        return ()
    name, separator, argument = query.partition(" ")
    if separator:
        if name.lower() != "/thinking" or len(argument.split()) > 1:
            return ()
        prefix = argument.strip().lower()
        return tuple(CommandHint(f"/thinking {level}", "当前模型支持的推理等级")
                     for level in thinking_levels if level.lower().startswith(prefix))
    return tuple(hint for hint in COMMANDS if hint.value.startswith(name.lower()))


@final
class CommandMenu(OptionList):
    """不夺取编辑焦点的提示列表；方向键由 Composer 转发，鼠标仍可直接选择。"""

    can_focus = False

    @final
    class Dismissed(Message):
        """菜单收起后同步输入区提示；不把 Esc 当成停止业务操作。"""

    def dismiss(self) -> None:
        self.display = False
        _ = self.post_message(self.Dismissed())

    def __init__(self) -> None:
        super().__init__(id="command-menu", markup=False, compact=True)
        self.hints: tuple[CommandHint, ...] = ()
        self._query = ""
        self.display = False

    def update_query(self, text: str, thinking_levels: Sequence[str] = ()) -> None:
        """thinking_levels 来自当前 Runtime；显示菜单本身不保存配置、不调用模型。"""
        self._query = text
        self.hints = command_hints(text, thinking_levels)
        _ = self.clear_options()
        if self.hints:
            _ = self.add_options(
                Option(Text.assemble((f"{hint.value:<14}  ", "bold"), (hint.description, "dim"),
                                     no_wrap=True, overflow="ellipsis"), id=hint.value)
                for hint in self.hints
            )
            self.highlighted = 0
        self.display = bool(self.hints)
        self.styles.height = min(6, len(self.hints)) + 2
        self.border_title = "指令"
        self.border_subtitle = "↑↓ · Tab · Enter · Esc"

    def selection_for(self, text: str) -> CommandHint | None:
        """只接受与当前输入相符的候选，防止快速粘贴/输入时执行尚未刷新的旧指令。"""
        return self.current if text == self._query else None

    @property
    def current(self) -> CommandHint | None:
        if not self.display or self.highlighted is None or self.highlighted >= len(self.hints):
            return None
        return self.hints[self.highlighted]
