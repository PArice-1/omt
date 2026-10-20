"""单进程、固定工作目录的 HTTP 应用工厂；资源只在 lifespan 内创建。"""

import hmac
import sqlite3
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from starlette.exceptions import HTTPException

from omt.coding_agent import bootstrap, settings as agent_settings
from omt.coding_agent.prompts import load_system_prompt
from omt.coding_agent.session_manager import SessionError, SessionManager
from omt.coding_agent.store import SessionStore

from .routes import session_routes
from .settings import ApiSettings


@asynccontextmanager
async def open_manager(options: ApiSettings) -> AsyncGenerator[SessionManager]:
    store = SessionStore(options.db_path)
    # 先确认索引表可读，缺库/缺表时不初始化模型或发现 MCP。
    _ = await store.list_sessions(options.workspace_path)
    system_prompt = load_system_prompt()
    model = bootstrap.build_model(agent_settings.load_settings())
    roots = options.skill_roots
    if roots is None:
        roots = [agent_settings.root / "agent" / "skills", options.workspace_path / "skills"]
    async with AsyncSqliteSaver.from_conn_string(str(options.db_path)) as saver:
        agent = await bootstrap.build_agent(
            model, checkpointer=saver,
            servers={name: server.connection() for name, server in options.mcp_servers.items()},
        )
        manager = SessionManager(
            agent, store, workspace_path=options.workspace_path, system_prompt=system_prompt,
            skill_roots=roots, max_concurrent_runs=options.max_concurrent_runs,
        )
        try:
            yield manager
        finally:
            await manager.aclose()


ManagerFactory = Callable[[ApiSettings], AbstractAsyncContextManager[SessionManager]]


def error_response(status: int, code: str, message: str, run_id: str | None = None) -> JSONResponse:
    error = {"code": code, "message": message}
    if run_id is not None:
        error["run_id"] = run_id
    return JSONResponse(
        status_code=status, content={"error": error},
        headers={"WWW-Authenticate": "Bearer"} if status == 401 else None,
    )


def create_app(
    options: ApiSettings | None = None, *, manager_factory: ManagerFactory = open_manager,
) -> FastAPI:
    options = options or ApiSettings()
    manager: SessionManager | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        nonlocal manager
        # 工具依赖进程 cwd。不能在请求中 chdir，也不能宣称 workspace 字段是沙箱。
        if options.workspace_path != Path.cwd().resolve() or not options.workspace_path.is_dir():
            raise ValueError("HTTP 工作目录必须与启动进程的当前目录一致，请先切换目录再启动")
        if not options.db_path.is_file():
            raise FileNotFoundError("请先创建 HTTP 会话数据库及 sessions 表")
        try:
            async with manager_factory(options) as active:
                manager = active
                yield
        finally:
            manager = None

    app = FastAPI(
        title="OMT", version="0.1.0", lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    bearer = HTTPBearer(auto_error=False)

    def authorize(
        credential: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        if credential is None or not hmac.compare_digest(
            credential.credentials.encode(), options.access_token.get_secret_value().encode(),
        ):
            raise HTTPException(status_code=401)

    def get_manager() -> SessionManager:
        if manager is None:
            raise SessionError("manager_closed", "服务尚未就绪或正在关闭")
        return manager

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(session_routes(get_manager, authorize))

    @app.exception_handler(SessionError)
    async def session_error(_request: Request, exc: SessionError) -> JSONResponse:
        statuses = {
            "session_not_found": 404, "session_busy": 409, "session_incomplete": 409,
            "capacity_exceeded": 429, "manager_closed": 503, "run_failed": 502,
            "storage_unavailable": 503,
        }
        return error_response(statuses.get(exc.code, 500), exc.code, str(exc), exc.run_id)

    @app.exception_handler(sqlite3.Error)
    async def storage_error(_request: Request, _exc: sqlite3.Error) -> JSONResponse:
        return error_response(503, "storage_unavailable", "会话存储暂不可用")

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        # 不返回 Pydantic 的 input/context，避免回显消息、路径或认证信息。
        return error_response(422, "invalid_request", "请求格式或字段不合法")

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        if exc.status_code == 401:
            return error_response(401, "unauthorized", "需要有效的 Bearer 访问令牌")
        return error_response(exc.status_code, "http_error", "请求无法处理")

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, _exc: Exception) -> JSONResponse:
        return error_response(500, "internal_error", "服务内部错误")

    return app


def main() -> None:
    """本机安全默认值；不启动额外 worker，不在导入时运行服务器。"""
    import uvicorn

    uvicorn.run("omt.api.app:create_app", factory=True, host="127.0.0.1", port=8000, workers=1)
