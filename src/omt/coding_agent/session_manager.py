"""固定工作目录的异步会话管理；检查点是跨请求历史来源，不维护全局当前会话。"""

import asyncio
import sqlite3
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AnyMessage
from langchain_core.runnables import RunnableConfig

from omt.agent.agent import Agent

from .runtime import CreateAgentSession
from .store import SessionRecord, SessionStore


class SessionError(Exception):
    """不携带底层异常原文的稳定业务错误，由传输层映射状态码。"""

    def __init__(self, code: str, message: str, *, run_id: str | None = None) -> None:
        super().__init__(message)
        self.code: str = code
        self.run_id: str | None = run_id


@dataclass(frozen=True)
class RunResult:
    session_id: str
    run_id: str
    answer: str
    warnings: tuple[str, ...] = ()


def validate_message(message: str) -> str:
    """保留有效输入原文，HTTP 与直接调用遵守相同约束。"""
    if not message.strip() or "\x00" in message or len(message) > 32000:
        raise ValueError("消息必须为 1～32000 字符的非空白文本，且不包含 NUL")
    return message


class SessionManager:
    """一个进程内的会话互斥、容量控制和运行清理。

    不缓存 Runtime：每次操作从最新检查点装载，避免内存历史过期。
    无检查点视为空会话；已有但未完成的检查点拒绝直接追加输入。
    锁不是多进程租约，不支持另一个 CLI/worker 同时写同一个会话。
    """

    def __init__(
        self,
        agent: Agent,
        store: SessionStore,
        *,
        workspace_path: Path,
        system_prompt: str,
        skill_roots: Sequence[Path],
        max_concurrent_runs: int = 1,
    ) -> None:
        if max_concurrent_runs < 1:
            raise ValueError("并发上限必须为正数")
        if agent.agent.checkpointer is None:
            raise ValueError("会话管理需要配置 Checkpointer")
        self._agent: Agent = agent
        self._store: SessionStore = store
        self._workspace: Path = workspace_path.expanduser().resolve()
        self._system_prompt: str = system_prompt
        self._skill_roots: tuple[Path, ...] = tuple(skill_roots)
        self._max_runs: int = max_concurrent_runs
        self._running: int = 0
        self._busy: set[str] = set()
        self._active: set[asyncio.Task[object]] = set()
        self._tainted: set[str] = set()
        self._closed: bool = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise SessionError("manager_closed", "服务正在关闭")

    async def create_session(self, title: str | None = None) -> SessionRecord:
        self._ensure_open()
        if title is not None and (len(title) > 200 or "\x00" in title):
            raise ValueError("标题最多 200 字符且不能包含 NUL")
        session_id = str(uuid4())
        await self._store.insert_session(session_id, self._workspace, title)
        return await self._record(session_id)

    async def list_sessions(self) -> list[SessionRecord]:
        self._ensure_open()
        return await self._store.list_sessions(self._workspace)

    async def _record(self, session_id: str) -> SessionRecord:
        record = await self._store.get_session(session_id)
        if record is None or record.workspace_path != str(self._workspace):
            raise SessionError("session_not_found", "会话不存在")
        return record

    @asynccontextmanager
    async def _operation(self, session_id: str, *, execution: bool = False) -> AsyncGenerator[None]:
        self._ensure_open()
        # 检查与占位之间不 await；同一事件循环内立即拒绝，不隐式排队。
        if session_id in self._busy:
            raise SessionError("session_busy", "该会话正在处理另一个操作")
        if execution and self._running >= self._max_runs:
            raise SessionError("capacity_exceeded", "当前运行数已达到上限，请稍后重试")
        self._busy.add(session_id)
        task = asyncio.current_task()
        if task is not None:
            self._active.add(task)
        if execution:
            self._running += 1
        try:
            yield
        finally:
            self._busy.discard(session_id)
            if task is not None:
                self._active.discard(task)
            if execution:
                self._running -= 1

    async def _load(self, session_id: str) -> CreateAgentSession:
        _ = await self._record(session_id)
        if session_id in self._tainted:
            raise SessionError("session_incomplete", "上次运行未完成，需人工检查检查点后恢复")
        config: RunnableConfig = {"configurable": {"thread_id": session_id}}
        snapshot = await self._agent.agent.aget_state(config)
        if snapshot.created_at is None:
            return CreateAgentSession(
                self._agent, config=config, system_prompt=self._system_prompt,
                skill_roots=self._skill_roots, event_handler=None,
            )
        conversation = CreateAgentSession(
            self._agent, config=config, skill_roots=[], event_handler=None,
        )
        try:
            await conversation.switch_session(session_id, display=False)
        except (ValueError, RuntimeError) as exc:
            raise SessionError("session_incomplete", "会话检查点未完成，不能直接追加输入") from exc
        return conversation

    async def get_history(self, session_id: str) -> list[AnyMessage]:
        async with self._operation(session_id):
            conversation = await self._load(session_id)
            return list(conversation.messages)

    async def run(self, session_id: str, message: str) -> RunResult:
        _ = validate_message(message)
        async with self._operation(session_id, execution=True):
            conversation = await self._load(session_id)
            run_id = str(uuid4())
            try:
                answer = await conversation.arun(message)
            except asyncio.CancelledError:
                self._tainted.add(session_id)
                raise
            except sqlite3.Error as exc:
                self._tainted.add(session_id)
                raise SessionError("storage_unavailable", "执行期间会话存储异常，请检查会话后恢复", run_id=run_id) from exc
            except Exception as exc:
                # 内存历史不提交，不代表工具副作用或检查点回滚；拒绝静默重试。
                self._tainted.add(session_id)
                raise SessionError("run_failed", "本轮执行失败，可能已有工具副作用，请检查会话", run_id=run_id) from exc
            warnings: tuple[str, ...] = ()
            try:
                _ = await self._store.update_session_updated_at(session_id)
            except (sqlite3.Error, ValueError):
                # 执行已经成功，不能以通用失败诱导客户端重试工具操作。
                warnings = ("session_timestamp_update_failed",)
            return RunResult(session_id, run_id, answer, warnings)

    async def aclose(self) -> None:
        """拒绝新操作，取消并等待活跃操作；由调用方随后关闭 Checkpointer。"""
        self._closed = True
        current = asyncio.current_task()
        tasks = [task for task in self._active if task is not current]
        for task in tasks:
            _ = task.cancel()
        if tasks:
            _ = await asyncio.gather(*tasks, return_exceptions=True)
