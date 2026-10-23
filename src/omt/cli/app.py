"""交互式 CLI 入口：负责输入、命令与展示，会话操作全部交给 Manager。"""

import asyncio
import sqlite3
import warnings
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from prompt_toolkit import PromptSession

from omt.core.bootstrap import SessionRuntime, open_session_runtime
from omt.core.session_manager import SessionError, SessionManager

from .commands import CliState, handle_command, handle_thinking_command
from .settings import CliSettings
from .terminal import display_thinking, print_event, print_warnings, read_input


# 默认工厂持有可切换的生命周期；兼容嵌入方直接提供固定 Manager。
open_manager = open_session_runtime
ManagerFactory = Callable[[CliSettings], AbstractAsyncContextManager[SessionRuntime | SessionManager]]


async def run_cli(
    options: CliSettings | None = None, *, manager_factory: ManagerFactory | None = None,
    terminal: PromptSession[str] | None = None,
) -> None:
    """在调用方的事件循环中运行交互；不读取 HTTP 令牌、不创建网络服务器。

    manager_factory 在整个输入循环期间持有 SessionRuntime 或固定 Manager，退出时
    负责关闭；默认 runtime 支持 /thinking 切换。固定 Manager 不读取额外模型配置，
    也不支持切换。terminal 可注入内存终端，默认创建一个 PromptSession。
    Ctrl-C/外部取消向外传播，由 main 或宿主负责处理；直接退出不登记空会话。
    """
    options = options or CliSettings()
    factory = manager_factory or open_manager
    async with factory(options) as active:
        runtime = active if isinstance(active, SessionRuntime) else None
        terminal = terminal if terminal is not None else PromptSession()
        state = CliState()
        print("输入 /resume 查看会话，/thinking 查看或修改推理强度，/exit 或 /quit 退出，Ctrl-C 或 Ctrl-D 结束会话。")
        if runtime is not None:
            display_thinking(runtime.thinking)
        while True:
            try:
                text = await read_input(terminal)
            except EOFError:
                print("\n会话结束。")
                return
            if await handle_thinking_command(text, runtime=runtime):
                continue
            manager = active.manager if isinstance(active, SessionRuntime) else active
            action = await handle_command(text, manager=manager, state=state)
            if action == "exit":
                print("会话结束。")
                return
            if action == "handled":
                continue
            if state.current is None:
                try:
                    state.current = await manager.create_session(title=text[:10])
                except sqlite3.Error:
                    print("[会话存储失败] 本轮尚未执行，可重新尝试登记。")
                    continue
            try:
                result = await manager.run(state.current.thread_id, text, event_handler=print_event)
            except sqlite3.Error:
                print("[会话存储失败] 暂时无法加载会话，本轮尚未执行。")
            except SessionError as exc:
                print(f"[会话执行失败] {exc}")
                print("可切换其他会话，或退出后检查恢复；未完成会话不会自动重跑。")
            else:
                # 回答已经通过事件显示，只展示非致命警告，不重复打印 answer。
                print_warnings(result.warnings)


def main() -> None:
    """同步脚本入口，仅在最外层创建一次事件循环。"""
    warnings.filterwarnings("ignore", message=r"The v3 streaming protocol on Pregel is experimental\.")
    try:
        asyncio.run(run_cli())
    except KeyboardInterrupt:
        print("\n会话结束。")
