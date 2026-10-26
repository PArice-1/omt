"""会话路由只承担参数、响应和鉴权依赖，不直接拼装 Agent 状态。"""

from collections.abc import AsyncIterator, Callable
from typing import Annotated

from fastapi import APIRouter, Depends

from omt.core.session_manager import SessionManager

from .runtime import ApiRuntime
from .streaming import RunStream, RunStreamingResponse
from .schemas import (
    CreateSessionRequest, MessageResponse, RunRequest, RunResponse, SessionResponse,
    project_history,
)


def session_routes(
    get_manager: Callable[[], AsyncIterator[SessionManager]], get_runtime: Callable[[], ApiRuntime],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/sessions", dependencies=[])

    @router.post("", status_code=201)
    async def create_session(body: CreateSessionRequest, manager: Annotated[SessionManager, Depends(get_manager)]) -> SessionResponse:
        return SessionResponse.from_record(await manager.create_session(body.title))

    @router.get("")
    async def list_sessions(manager: Annotated[SessionManager, Depends(get_manager)]) -> list[SessionResponse]:
        return [SessionResponse.from_record(record) for record in await manager.list_sessions()]

    @router.get("/{session_id}/messages", response_model_exclude_none=True)
    async def get_messages(session_id: str, manager: Annotated[SessionManager, Depends(get_manager)]) -> list[MessageResponse]:
        return project_history(await manager.get_history(session_id))

    @router.post("/{session_id}/runs")
    async def run(session_id: str, body: RunRequest, manager: Annotated[SessionManager, Depends(get_manager)]) -> RunResponse:
        result = await manager.run(session_id, body.message)
        return RunResponse(
            session_id=result.session_id, run_id=result.run_id, answer=result.answer,
            warnings=list(result.warnings),
        )

    @router.post("/{session_id}/runs/stream", response_class=RunStreamingResponse, status_code=200,
                 responses={200: {"content": {"text/event-stream": {}}, "description": "版本 1 的 SSE 事件流"}})
    async def stream_run(
        session_id: str, body: RunRequest,
        manager: Annotated[SessionManager, Depends(get_manager)],
        runtime: Annotated[ApiRuntime, Depends(get_runtime)],
    ) -> RunStreamingResponse:
        stream = RunStream(manager, session_id, body.message, runtime.options, runtime.thinking())
        await stream.start()
        return RunStreamingResponse(stream)

    return router
