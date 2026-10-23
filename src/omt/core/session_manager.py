"""固定工作目录的异步会话管理；检查点是跨请求历史来源，不维护全局当前会话。"""

import asyncio
import sqlite3
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AnyMessage
from langchain_core.runnables import RunnableConfig

from omt.agent.agent import Agent

from .conversation import Conversation
from .events import EventHandler
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


@dataclass(frozen=True)
class LoadedSession:
    """激活会话后的索引、原始历史和非致命警告；不包含入口的当前会话状态。"""

    record: SessionRecord
    messages: list[AnyMessage]
    warnings: tuple[str, ...] = ()


def validate_message(message: str) -> str:
    """保留有效输入原文，HTTP 与直接调用遵守相同约束。"""
    if not message.strip() or "\x00" in message or len(message) > 32000:
        raise ValueError("消息必须为 1～32000 字符的非空白文本，且不包含 NUL")
    return message


class SessionManager:
    """一个进程内的会话互斥、容量控制和运行清理。

    不缓存 Conversation：每次操作从最新检查点装载，避免内存历史过期。
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
        """agent 必须配置 Checkpointer；store 保存同一组 thread_id 的会话索引。

        workspace_path 用于索引归属检查，不执行 chdir 或沙箱隔离。
        system_prompt 是空会话首轮使用的正文；skill_roots 是 Skills 搜索目录，
        [] 关闭发现。已有历史沿用其原始系统消息，不重新加载提示词/Skills。
        max_concurrent_runs 是本进程允许同时处理的 run 数，满载立即拒绝，
        不限制图内部的工具并发，也不保护另一个进程的同会话写入。
        """
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
        self._running: int = 0  # 已接纳、尚未结束的 run 操作数。
        self._busy: set[str] = set()  # 正在运行、读取或激活的会话 ID，同会话不能重入。
        self._active: set[asyncio.Task[object]] = set()  # 关闭时需要取消并等待的操作任务。
        self._tainted: set[str] = set()  # 本进程观察到失败/取消、需检查恢复的会话 ID。
        self._closed: bool = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise SessionError("manager_closed", "服务正在关闭")

    async def create_session(self, title: str | None = None) -> SessionRecord:
        """在sessions 数据库中，保存这一次创建的session的数据"""
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

    async def _load(
        self, session_id: str, *, event_handler: EventHandler | None = None,
    ) -> Conversation:
        """加载最新历史；event_handler 仅绑定本次调用，不影响同 Manager 的其他入口。"""
        _ = await self._record(session_id)
        if session_id in self._tainted:
            raise SessionError("session_incomplete", "上次运行未完成，需人工检查检查点后恢复")
        config: RunnableConfig = {"configurable": {"thread_id": session_id}}
        snapshot = await self._agent.agent.aget_state(config)
        if snapshot.created_at is None:
            return Conversation(
                self._agent, config=config, system_prompt=self._system_prompt,
                skill_roots=self._skill_roots, event_handler=event_handler,
            )
        conversation = Conversation(
            self._agent, config=config, skill_roots=[], event_handler=event_handler,
        )
        try:
            await conversation.load_history(session_id)
        except (ValueError, RuntimeError) as exc:
            raise SessionError("session_incomplete", "会话检查点未完成，不能直接追加输入") from exc
        return conversation

    async def get_history(self, session_id: str) -> list[AnyMessage]:
        async with self._operation(session_id):
            conversation = await self._load(session_id)
            return list(conversation.messages)

    async def _touch(self, session_id: str) -> tuple[str | None, tuple[str, ...]]:
        """操作已完成后更新索引；只返回警告，避免更新时间失败诱导重跑工具。"""
        try:
            return await self._store.update_session_updated_at(session_id), ()
        except (sqlite3.Error, ValueError):
            return None, ("session_timestamp_update_failed",)

    async def open_session(self, session_id: str) -> LoadedSession:
        """校验目录和历史后激活指定 ID，刷新索引时间；不执行模型、不维护当前 ID。"""
        async with self._operation(session_id):
            record = await self._record(session_id)
            conversation = await self._load(session_id)
            updated_at, warnings = await self._touch(session_id)
            if updated_at is not None:
                record = replace(record, updated_at=updated_at)
            return LoadedSession(record, list(conversation.messages), warnings)

    async def run(
        self, session_id: str, message: str, *, event_handler: EventHandler | None = None,
    ) -> RunResult:
        """执行指定会话的一轮；event_handler 接收快速同步通知，None 不展示过程。

        会话互斥、容量、历史和失败规则与入口无关；接收函数只影响本次执行，
        CLI 可传终端渲染函数，HTTP 可保持静默。取消不保证工具副作用撤销。
        """
        _ = validate_message(message)
        async with self._operation(session_id, execution=True):
            conversation = await self._load(session_id, event_handler=event_handler)
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
            _, warnings = await self._touch(session_id)
            return RunResult(session_id, run_id, answer, warnings)

    async def rebuild(self, agent: Agent) -> "SessionManager":
        """空闲时用新 agent 创建继任 Manager，成功后关闭当前实例。

        agent 必须使用同一个 Checkpointer；Store、目录、提示词、Skills、容量及
        失败会话保护完整保留。不复制历史、不登记会话。有运行/历史/激活操作时
        立即拒绝，不为切换取消请求。生命周期由入口顺序管理；构建失败时当前
        实例仍可用，成功后调用方须改用返回的新实例。
        """
        self._ensure_open()
        if self._busy or self._active:
            raise SessionError("manager_busy", "有会话操作正在执行，暂时不能切换模型")
        if agent.agent.checkpointer is not self._agent.agent.checkpointer:
            raise ValueError("重建 Manager 必须复用同一个 Checkpointer")
        successor = SessionManager(
            agent, self._store, workspace_path=self._workspace,
            system_prompt=self._system_prompt, skill_roots=self._skill_roots,
            max_concurrent_runs=self._max_runs,
        )
        successor._tainted.update(self._tainted)
        # 空闲检查至交接之间不让出事件循环；旧实例不再接纳操作。
        await self.aclose()
        return successor

    async def aclose(self) -> None:
        """拒绝新操作，取消并等待活跃操作；由调用方随后关闭 Checkpointer。"""
        self._closed = True
        current = asyncio.current_task()
        tasks = [task for task in self._active if task is not current]
        for task in tasks:
            _ = task.cancel()
        if tasks:
            _ = await asyncio.gather(*tasks, return_exceptions=True)
