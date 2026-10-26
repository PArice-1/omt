"""单用户本机 HTTP 入口；无配置可启动，资源只在 lifespan 内创建。"""

import sqlite3
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from omt.core import bootstrap
from omt.core.session_manager import SessionError, SessionManager

from .configuration_routes import configuration_routes
from .routes import session_routes
from .runtime import ApiRuntime, RuntimeFactory
from .schemas import HttpErrorResponse
from .settings import ApiSettings


@asynccontextmanager
async def open_manager(options: ApiSettings) -> AsyncGenerator[SessionManager]:
    async with bootstrap.open_session_manager(options) as manager:
        yield manager


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
    options: ApiSettings | None = None, *, manager_factory: ManagerFactory | None = None,
    runtime_factory: RuntimeFactory | None = None,
) -> FastAPI:
    """默认持有可切换 Runtime；manager_factory 保留固定 Manager 的嵌入兼容。

    固定 Manager 模式不读取模型配置，也不支持首次配置或切换；两种工厂不能同时注入。
    """
    options = options or ApiSettings()
    if manager_factory is not None and runtime_factory is not None:
        raise ValueError("不能同时注入 Manager 和 Runtime 工厂")
    active_factory = runtime_factory or bootstrap.open_session_runtime
    runtime: ApiRuntime | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        nonlocal runtime
        try:
            if manager_factory is not None:
                async with manager_factory(options) as manager:
                    runtime = ApiRuntime(options, active_factory, fixed_manager=manager)
                    try:
                        await runtime.start()
                        yield
                    finally:
                        await runtime.aclose()
            else:
                runtime = ApiRuntime(options, active_factory)
                try:
                    await runtime.start()
                    yield
                finally:
                    await runtime.aclose()
        finally:
            runtime = None

    app = FastAPI(
        title="OMT", version="0.1.0", lifespan=lifespan,
        responses={422: {"model": HttpErrorResponse, "description": "请求字段或选择不合法，不回显原始输入"}},
    )

    def get_runtime() -> ApiRuntime:
        if runtime is None:
            raise SessionError("manager_closed", "服务尚未就绪或正在关闭")
        return runtime

    async def get_manager() -> AsyncGenerator[SessionManager]:
        owner = get_runtime()
        manager = await owner.acquire_manager()
        try:
            yield manager
        finally:
            owner.release_manager()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(configuration_routes(get_runtime))
    app.include_router(session_routes(get_manager, get_runtime))

    @app.exception_handler(SessionError)
    async def session_error(_request: Request, exc: SessionError) -> JSONResponse:
        statuses = {
            "session_not_found": 404, "session_busy": 409, "session_incomplete": 409,
            "capacity_exceeded": 429, "manager_closed": 503, "run_failed": 502,
            "storage_unavailable": 503, "runtime_not_ready": 503, "manager_busy": 409,
            "runtime_immutable": 409, "configuration_invalid": 409,
            "invalid_model": 422, "invalid_thinking_level": 422,
            "model_change_failed": 502, "login_preview_failed": 422,
            "login_capacity_exceeded": 429, "login_preview_not_found": 404,
            "configuration_save_failed": 503,
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
