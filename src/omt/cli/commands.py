"""CLI 命令与当前选择状态；会话查询和恢复只调用 Manager。"""

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from omt.coding_agent.session_manager import SessionError, SessionManager
from omt.coding_agent.store import SessionRecord

from .terminal import display_history, print_warnings


@dataclass
class CliState:
    """current=None 表示尚未登记的新对话；编号只绑定最近展示的那次列表。"""

    current: SessionRecord | None = None
    started_at: str = field(default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"))
    resume_options: dict[int, str] = field(default_factory=dict)


def display_sessions(records: list[SessionRecord], state: CliState) -> None:
    """0 固定表示当前会话，其他编号沿用 Store 排序，不因之后新增记录重新映射。"""
    current = state.current
    if current is not None:
        current = next((record for record in records if record.thread_id == current.thread_id), current)
    title = current.title if current is not None else "新对话"
    title = " ".join((title or "未命名对话").split())
    updated_at = current.updated_at if current is not None else state.started_at
    print("number | title | updated_at")
    print(f"0 | {title}（当前） | {updated_at}")
    others = [record for record in records if current is None or record.thread_id != current.thread_id]
    for number, record in enumerate(others, start=1):
        title = " ".join((record.title or "未命名对话").split())
        print(f"{number} | {title} | {record.updated_at}")
        state.resume_options[number] = record.thread_id


async def handle_command(
    text: str, *, manager: SessionManager, state: CliState,
) -> Literal["run", "handled", "exit"]:
    """识别退出和 resume 命令；run 表示原文应作为普通消息，命令本身不执行模型。

    state 只保存当前 CLI 的选择，Manager 不维护全局当前会话；恢复失败时
    保持旧选择。列表刷新和成功恢复会使旧编号失效，防止误选其他会话。
    """
    if text.strip().lower() in {"/exit", "/quit"}:
        return "exit"
    words = text.strip().split()
    if not words or words[0].lower() not in {"/resume", "resume"}:
        return "run"
    if len(words) == 1:
        state.resume_options.clear()
        try:
            records = await manager.list_sessions()
        except (sqlite3.Error, SessionError):
            print("[会话查询失败] 暂时无法读取会话列表。")
        else:
            display_sessions(records, state)
        return "handled"
    if len(words) != 2:
        print("用法：/resume 查看列表，/resume N 恢复编号 N 的会话。")
        return "handled"
    try:
        number = int(words[1])
    except ValueError:
        print("用法：/resume N，N 必须是会话编号。")
        return "handled"
    if number == 0:
        print("0 是当前会话，无需恢复。")
        return "handled"
    if number not in state.resume_options:
        print("编号无效，请先输入 /resume 查看会话列表。")
        return "handled"
    try:
        opened = await manager.open_session(state.resume_options[number])
    except SessionError as exc:
        if exc.code == "session_not_found":
            state.resume_options.clear()
            print("会话不存在或不属于当前目录，请重新输入 /resume。")
        else:
            print(f"[会话恢复失败] {exc}")
        return "handled"
    except sqlite3.Error:
        print("[会话恢复失败] 暂时无法读取会话存储。")
        return "handled"
    state.current = opened.record
    state.resume_options.clear()
    display_history(opened.messages)
    print_warnings(opened.warnings)
    print("会话已恢复；输入 /resume 查看更新后的编号。")
    return "handled"
