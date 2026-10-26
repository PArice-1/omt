"""一次请求拥有一次运行的 SSE Adapter；有限缓冲、发送超时和断线清理。"""

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from typing import override

from anyio import CancelScope
from fastapi.responses import StreamingResponse
from langchain_core.messages import ToolMessage
from pydantic import JsonValue, TypeAdapter
from starlette.types import Message, Receive, Scope, Send

from omt.core.events import RunEvent
from omt.core.session_manager import SessionError, SessionManager

from .schemas import StreamEvent, ThinkingResponse
from .settings import ApiSettings


_JSON = TypeAdapter(dict[str, JsonValue])


def _json(value: object) -> JsonValue:
    """只接受可表示的 JSON 值，不通过 str/repr 隐式暴露内部对象。"""
    return _JSON.validate_python({"value": value})["value"]


class RunStream:
    """管理运行任务与 SSE 缓冲，不保存后台任务台账。

    settings 限制积压事件数、编码后字节数和每次网络发送时间；单条事件超过
    字节上限也终止。thinking 是接纳前取得的运行配置快照，运行占位后不能切换。
    """

    def __init__(
        self, manager: SessionManager, session_id: str, message: str,
        settings: ApiSettings, thinking: ThinkingResponse | None,
    ) -> None:
        self.manager: SessionManager = manager
        self.session_id: str = session_id
        self.message: str = message
        self.settings: ApiSettings = settings
        self.thinking: ThinkingResponse | None = thinking
        self.run_id: str = ""
        self._seq: int = 0
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._bytes: int = 0
        self._accepted: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._task: asyncio.Task[None] | None = None
        self._terminal: bytes | None = None
        self._failure: str | None = None

    def _encode(
        self, kind: str, data: dict[str, JsonValue], *,
        role: str | None = None, message_id: str | None = None, tool_call_id: str | None = None,
    ) -> bytes:
        self._seq += 1
        event = StreamEvent(
            seq=self._seq, type=kind, session_id=self.session_id, run_id=self.run_id,
            role="assistant" if role == "assistant" else "tool" if role == "tool" else None,
            message_id=message_id, tool_call_id=tool_call_id, data=data,
        )
        return (f"event: {kind}\ndata: {event.model_dump_json(exclude_none=True)}\n\n").encode("utf-8")

    def _abort(self, code: str) -> None:
        if self._failure is None:
            self._failure = code
            if self._task is not None:
                _ = self._task.cancel()

    def _enqueue(self, event: bytes) -> None:
        if self._failure is not None:
            return
        if (self._queue.qsize() >= self.settings.stream_buffer_events or
                self._bytes + len(event) > self.settings.stream_buffer_bytes):
            self._abort("stream_buffer_exceeded")
            return
        self._bytes += len(event)
        self._queue.put_nowait(event)

    def _started(self, run_id: str) -> None:
        self.run_id = run_id
        snapshot: JsonValue = None if self.thinking is None else _json(self.thinking.model_dump())
        self._enqueue(self._encode("run.started", {"thinking": snapshot}))
        self._accepted.set_result(None)

    def _event(self, event: RunEvent) -> None:
        # 初始化诊断和 core 的异常原文不直接暴露到网络；运行终态由 Manager 结果生成。
        if not self.run_id or self._failure is not None or event.kind in {"skills.diagnostic", "run.failed"}:
            return
        kind = "assistant.text.delta" if event.kind == "assistant.delta" else event.kind
        try:
            data: dict[str, JsonValue] = {}
            role = "assistant" if kind.startswith("assistant.") else "tool"
            if kind.endswith(".delta") and role == "assistant":
                data["delta"] = event.text
            elif role == "tool":
                data["name"] = event.tool_name
                if kind == "tool.started":
                    data["input"] = _json(event.data)
                elif kind == "tool.delta":
                    data["delta"] = _json(event.data)
                elif isinstance(event.data, ToolMessage):
                    data["text"] = event.data.text
                    data["status"] = event.data.status
                elif kind == "tool.failed":
                    data["code"] = "tool_failed"
                    data["message"] = "工具执行失败"
                else:
                    data["output"] = _json(event.data)
            self._enqueue(self._encode(kind, data, role=role, message_id=event.message_id,
                                       tool_call_id=event.tool_call_id))
        except Exception:
            self._abort("invalid_stream_data")

    def _end(self, kind: str, data: dict[str, JsonValue]) -> None:
        encoded = self._encode(kind, data)
        if len(encoded) > self.settings.stream_buffer_bytes:
            encoded = self._encode("run.failed", {"code": "stream_event_too_large",
                                                  "message": "终态内容超过传输上限，请检查历史，勿自动重试"})
        self._terminal = encoded

    async def _run(self) -> None:
        try:
            result = await self.manager.run(self.session_id, self.message, event_handler=self._event,
                                            on_started=self._started)
            self._end("run.completed", {"status": "completed", "answer": result.answer,
                                        "warnings": list(result.warnings)})
        except asyncio.CancelledError:
            if not self._accepted.done():
                _ = self._accepted.cancel()
            if self.run_id:
                kind = "run.failed" if self._failure is not None else "run.cancelled"
                self._end(kind, {"code": self._failure or "run_cancelled",
                                 "message": "运行已中止，可能已有工具副作用，请检查会话"})
        except sqlite3.Error:
            error = SessionError("storage_unavailable", "会话存储暂不可用")
            if not self._accepted.done():
                self._accepted.set_exception(error)
            else:
                self._end("run.failed", {"code": error.code, "message": str(error)})
        except SessionError as exc:
            if not self._accepted.done():
                self._accepted.set_exception(exc)
            else:
                self._end("run.failed", {"code": exc.code, "message": str(exc)})
        except Exception:
            error = SessionError("internal_error", "运行处理失败，请检查会话")
            if not self._accepted.done():
                self._accepted.set_exception(error)
            else:
                self._end("run.failed", {"code": error.code, "message": str(error)})
        finally:
            # 一个额外唤醒标记不含业务正文，不受缓冲配额约束，避免空队列等待终态。
            self._queue.put_nowait(None)

    async def start(self) -> None:
        """在发送响应头前等待接纳结果；失败/请求取消时回收运行任务。"""
        self._task = asyncio.create_task(self._run())
        try:
            await self._accepted
        except BaseException:
            await self.aclose()
            raise

    async def events(self) -> AsyncIterator[bytes]:
        try:
            while True:
                try:
                    item = await asyncio.wait_for(self._queue.get(), self.settings.stream_heartbeat)
                except TimeoutError:
                    yield b": heartbeat\n\n"
                    continue
                if item is None:
                    break
                self._bytes -= len(item)
                yield item
            if self._terminal is not None:
                yield self._terminal
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """请求结束即取消并等待关联运行；在 ASGI 取消域内也必须等待清理。"""
        with CancelScope(shield=True):
            task = self._task
            if task is not None and not task.done():
                _ = task.cancel()
            if task is not None:
                _ = await asyncio.gather(task, return_exceptions=True)


class RunStreamingResponse(StreamingResponse):
    """在响应整体生命周期中拥有 RunStream，包括首个发送就失败的路径。"""

    def __init__(self, stream: RunStream) -> None:
        self.stream: RunStream = stream
        super().__init__(stream.events(), media_type="text/event-stream",
                         headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @override
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_timeout(message: Message) -> None:
            await asyncio.wait_for(send(message), timeout=self.stream.settings.stream_send_timeout)

        try:
            await super().__call__(scope, receive, send_with_timeout)
        finally:
            await self.stream.aclose()
