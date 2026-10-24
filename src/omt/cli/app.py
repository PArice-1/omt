"""交互式 CLI 入口：负责输入、命令与展示，会话操作全部交给 Manager。"""

import asyncio
import sqlite3
import warnings
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from pathlib import Path

from prompt_toolkit import PromptSession

from omt.core import model_configuration
from omt.core.bootstrap import SessionRuntime, open_session_runtime
from omt.core.session_manager import SessionError, SessionManager
from omt.core.store import initialize_session_database

from .commands import CliState, handle_command, handle_thinking_command
from .login import login
from .settings import CliSettings
from .terminal import display_thinking, print_event, print_warnings, read_input


# 默认工厂持有可切换的生命周期；兼容嵌入方直接提供固定 Manager。
open_manager = open_session_runtime
ManagerFactory = Callable[[CliSettings], AbstractAsyncContextManager[SessionRuntime | SessionManager]]


async def run_cli(
    options: CliSettings | None = None, *, manager_factory: ManagerFactory | None = None,
    terminal: PromptSession[str] | None = None,
) -> None:
    """在调用方事件循环中运行交互；没有模型时仍允许 /login 和退出。

    manager_factory 在整个输入循环期间持有 SessionRuntime 或固定 Manager，退出时
    负责关闭。默认工厂先通过 core 检查本地配置，不完整时只提示 /login，登录后才
    创建缺失数据库并组装运行环境；已有完整配置仍要求预建库。自定义工厂绕过检查。
    terminal 可注入内存终端。/login 可随时保存配置，但不替换已有运行模型；
    首次登录补齐默认选择后可立即使用。Ctrl-C/外部取消向上传播，不登记空会话。
    """
    options = options or CliSettings()
    factory = manager_factory or open_manager
    terminal = terminal if terminal is not None else PromptSession()
    default_factory = factory is open_session_runtime
    if default_factory and options.workspace_path != Path.cwd().resolve():
        raise ValueError("工作目录必须与启动进程的当前目录一致，请先切换目录再启动")
    active: SessionRuntime | SessionManager | None = None
    async with AsyncExitStack() as stack:
        if default_factory:
            status = model_configuration.check_model_configuration()
            if not status.ready:
                print(f"{status.message} 请输入 /login 添加 API 模型。")
            else:
                active = await stack.enter_async_context(factory(options))
        else:
            active = await stack.enter_async_context(factory(options))
        state = CliState()
        print("输入 /login 添加模型，/resume 查看会话，/thinking 查看或修改推理强度，/exit 或 /quit 退出，Ctrl-C 或 Ctrl-D 结束会话。")
        if isinstance(active, SessionRuntime):
            display_thinking(active.thinking)
        while True:
            try:
                text = await read_input(terminal)
            except EOFError:
                print("\n会话结束。")
                return
            words = text.strip().split()
            if text.strip().lower() in {"/exit", "/quit"}:
                print("会话结束。")
                return
            if words and words[0].lower() == "/login":
                if len(words) != 1:
                    print("用法：/login，随后按提示输入厂商和模型参数；不要在命令中输入 Key。")
                    continue
                result = await login(terminal)
                if result is not None:
                    if active is not None:
                        print("本次运行继续使用原模型，不自动重建；新配置供后续初始化使用。")
                    else:
                        status = model_configuration.check_model_configuration()
                        if not status.ready:
                            print(f"{status.message} 请继续 /login 补齐默认模型。")
                            continue
                        try:
                            initialize_session_database(options.db_path)
                            active = await stack.enter_async_context(factory(options))
                        except Exception:
                            print("[模型初始化失败] 配置已保存，请检查数据库或模型连接后再次 /login。")
                        else:
                            if isinstance(active, SessionRuntime):
                                display_thinking(active.thinking)
                continue
            if active is None:
                print("尚无可用模型，请先输入 /login 添加 API 模型，或 /exit 退出。")
                continue
            runtime = active if isinstance(active, SessionRuntime) else None
            if await handle_thinking_command(text, runtime=runtime):
                continue
            manager = active.manager if isinstance(active, SessionRuntime) else active
            action = await handle_command(text, manager=manager, state=state)
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
