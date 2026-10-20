"""交互入口：读取配置、组装依赖，在同一事件循环中顺序处理异步回合。"""

import asyncio
import sqlite3
import warnings
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from langchain_core.runnables import RunnableConfig

from prompt_toolkit import PromptSession
from prompt_toolkit.document import Document
from prompt_toolkit.validation import ValidationError, Validator
from langchain_mcp_adapters.sessions import Connection
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.checkpoint.base import BaseCheckpointSaver

from omt.agent.agent import Agent
from omt.ai.model import Model

from .prompts import load_system_prompt
from .runtime import CreateAgentSession
from .settings import load_settings, root
from .bootstrap import build_model as _get_model, build_agent
from .mcp import get_tools
from .store import SessionRecord, SessionStore

## ============= const varibal=============



#mcp server
knowledge_root = Path("/home/laterain/Projects/Agent/rag-mcp")
# mcp servers
servers: dict[str, Connection] = {
    "knowledge": {
        "transport": "stdio",
        "command": str(knowledge_root / ".venv" / "bin" / "python"),
        "args": ["-m", "rag_mcp.server"],
        "cwd": str(knowledge_root),
    }
}

# db path
db_path = (root / "agent" / "state" / "omt.sqlite").expanduser().resolve()

## =============== end ===================


async def _get_agent(
    model: Model,
    *,
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> Agent:
    """用底层聊天模型和内置工具创建可执行的 Agent 实例。"""
    mcp_tools = await get_tools(servers=servers)
    return await build_agent(model, checkpointer=checkpointer, extra_tools=mcp_tools)


class _UserInputValidator(Validator):
    """在用户提交输入时校验，保留合法输入的原始空格和换行。"""

    def validate(self, document: Document) -> None:
        text = document.text
        if not text.strip():
            raise ValidationError(message="输入不能为空或只有空白", cursor_position=len(text))
        if "\x00" in text:
            raise ValidationError(message="输入不能包含 NUL 字符", cursor_position=text.index("\x00"))


def _get_user_input(terminal: PromptSession[str] | None = None) -> str:
    """同步读取有效输入；Ctrl-C/EOF 交给会话循环处理，不使用内置 input()。"""
    if terminal is None:
        terminal = PromptSession()
    return terminal.prompt(
        "You> ",
        validator=_UserInputValidator(),
        validate_while_typing=False,
    )


async def _aget_user_input(terminal: PromptSession[str] | None = None) -> str:
    """异步读取有效输入；保留同步输入函数的校验和控制信号语义。"""
    if terminal is None:
        terminal = PromptSession()
    return await terminal.prompt_async(
        "You> ",
        validator=_UserInputValidator(),
        validate_while_typing=False,
    )


def _display_resume_sessions(
    records: list[SessionRecord],
    current_thread_id: str,
    current_updated_at: str,
) -> dict[int, str]:
    """固定当前会话为 0，其他会话沿用 Store 的最近修改优先顺序。"""
    current = next((record for record in records if record.thread_id == current_thread_id), None)
    title = current.title if current is not None else "新对话"
    title = " ".join((title or "未命名对话").split())
    updated_at = current.updated_at if current is not None else current_updated_at
    print("number | title | updated_at")
    print(f"0 | {title}（当前） | {updated_at}")

    options = {0: current_thread_id}
    others = [record for record in records if record.thread_id != current_thread_id]
    for number, record in enumerate(others, start=1):
        title = " ".join((record.title or "未命名对话").split())
        print(f"{number} | {title} | {record.updated_at}")
        options[number] = record.thread_id
    return options


async def _check_user_input(
    user_input: str,
    *,
    conversation: CreateAgentSession,
    session_store: SessionStore,
    workspace_path: Path,
    thread_id: str,
    current_updated_at: str,
    resume_options: dict[int, str],
) -> tuple[Literal["run", "handled", "exit"], SessionRecord | None]:
    """处理 CLI 命令，返回下一步动作和成功恢复的会话记录。

    run 表示普通请求，handled 表示命令已处理，exit 表示退出。
    原地更新编号映射；入口负责根据恢复记录同步 ID 和登记状态。
    """
    if user_input.strip().lower() in {"/exit", "/quit"}:
        print("会话结束。")
        return "exit", None

    command = user_input.strip().split()
    # 无斜杠的 resume 也作为命令处理，避免误输入触发模型和工具。
    if not command or command[0].lower() not in {"/resume", "resume"}:
        return "run", None

    if len(command) == 1:
        resume_options.clear()
        try:
            records = await session_store.list_sessions(workspace_path)
        except sqlite3.Error as exc:
            print(f"[会话查询失败] {exc}")
        else:
            resume_options.update(_display_resume_sessions(
                records, thread_id, current_updated_at,
            ))
        return "handled", None
    if len(command) != 2:
        print("用法：/resume 查看列表，/resume N 恢复编号 N 的会话。")
        return "handled", None
    try:
        number = int(command[1])
    except ValueError:
        print("用法：/resume N，N 必须是会话编号。")
        return "handled", None
    if number == 0:
        print("0 是当前会话，无需恢复。")
        return "handled", None
    if number not in resume_options:
        print("编号无效，请先输入 /resume 查看会话列表。")
        return "handled", None

    try:
        # 使用已展示的编号，不因新增记录改变编号含义；恢复前复查目录归属。
        record = await session_store.get_session(resume_options[number])
        if record is None or record.workspace_path != str(workspace_path):
            resume_options.clear()
            print("会话不存在或不属于当前目录，请重新输入 /resume。")
            return "handled", None
        await conversation.switch_session(record.thread_id)
    except (sqlite3.Error, ValueError, RuntimeError) as exc:
        print(f"[会话恢复失败] {exc}")
        return "handled", None
    # 当前会话变更后旧编号作废，必须重新展示，避免静默重映射。
    resume_options.clear()
    try:
        updated_at = await session_store.update_session_updated_at(record.thread_id)
    except (sqlite3.Error, ValueError) as exc:
        # Runtime 已经切换，时间写入失败也必须让入口同步目标 ID 和登记状态。
        print(f"[修改时间更新失败] {exc}；会话已切换，可继续对话。")
    else:
        record = replace(record, updated_at=updated_at)
        print("会话已恢复；输入 /resume 查看更新后的编号。")
    return "handled", record


async def asession() -> None:
    """组装会话配置和存储；在同一事件循环中处理普通请求与会话切换。"""
    settings = load_settings()
    system_prompt = load_system_prompt()
    if not db_path.is_file():
        raise FileNotFoundError(f"会话数据库不存在，请先创建数据库和 sessions 表：{db_path}")

    workspace_path = Path.cwd().resolve()
    thread_id = str(uuid4())
    current_updated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    session_store = SessionStore(db_path)
    model = _get_model(settings)

    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as checkpointer:
        agent = await _get_agent(model, checkpointer=checkpointer)
        conversation = CreateAgentSession(
            agent,
            config=config,
            system_prompt=system_prompt,
        )
        terminal: PromptSession[str] = PromptSession()
        print("输入 /resume（或 resume）查看会话，/exit 或 /quit 退出，Ctrl-C 或 Ctrl-D 结束会话。")
        session_registered = False
        resume_options: dict[int, str] = {}

        while True:
            try:
                user_input = await _aget_user_input(terminal)
            except EOFError:
                print("\n会话结束。")
                break

            action, restored_session = await _check_user_input(
                user_input,
                conversation=conversation,
                session_store=session_store,
                workspace_path=workspace_path,
                thread_id=thread_id,
                current_updated_at=current_updated_at,
                resume_options=resume_options,
            )
            if action == "exit":
                break
            if restored_session is not None:
                thread_id = restored_session.thread_id
                current_updated_at = restored_session.updated_at
                session_registered = True
            if action == "handled":
                continue

            if not session_registered:
                try:
                    # 先保存索引，失败或取消产生的检查点才能通过同一 ID 找到。
                    await session_store.insert_session(
                        thread_id=thread_id,
                        workspace_path=workspace_path,
                        title=user_input[:10],
                    )
                except sqlite3.Error as exc:
                    print(f"[会话存储失败] {exc}；本轮尚未执行，可重试。")
                    continue
                session_registered = True

            try:
                # runtime 已负责流式展示，不再把返回的最终回答重复打印一次。
                await conversation.arun(user_input)
            except Exception:
                # 这里只处理执行失败，不把索引写入错误误报为模型执行失败。
                print("本轮执行未完成，可继续输入，或使用 /exit 退出。")


def session() -> None:
    """只在最外层创建事件循环；Ctrl-C 等待异步清理后退出，不打印堆栈。"""
    try:
        asyncio.run(asession())
    except KeyboardInterrupt:
        print("\n会话结束。")


# 在模块入口启动前设置；仅隐藏实验性提示，不改变协议行为。
warnings.filterwarnings(
    "ignore",
    message=r"The v3 streaming protocol on Pregel is experimental\.",
)


if __name__ == "__main__":
    session()