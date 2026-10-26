"""运行信息、临时配置预览与运行设置路由。"""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends

from .runtime import ApiRuntime
from .schemas import (
    LoginConfirmRequest, LoginConfirmResponse, LoginPreviewRequest, LoginPreviewResponse,
    ModelRequest, ModelResponse, ProviderResponse, RuntimeResponse, ThinkingRequest,
)


def configuration_routes(get_runtime: Callable[[], ApiRuntime]) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.get("/runtime")
    async def status(runtime: Annotated[ApiRuntime, Depends(get_runtime)]) -> RuntimeResponse:
        return runtime.status()

    @router.get("/providers")
    async def providers(runtime: Annotated[ApiRuntime, Depends(get_runtime)]) -> list[ProviderResponse]:
        return runtime.providers()

    @router.get("/models")
    async def models(runtime: Annotated[ApiRuntime, Depends(get_runtime)]) -> list[ModelResponse]:
        return runtime.models()

    @router.post("/model-logins", status_code=201)
    async def preview(body: LoginPreviewRequest, runtime: Annotated[ApiRuntime, Depends(get_runtime)]) -> LoginPreviewResponse:
        return await runtime.preview(body.provider, body.api_key)

    @router.post("/model-logins/{login_id}/confirm")
    async def confirm(login_id: str, body: LoginConfirmRequest, runtime: Annotated[ApiRuntime, Depends(get_runtime)]) -> LoginConfirmResponse:
        return await runtime.confirm(login_id, body.model)

    @router.put("/runtime/model")
    async def model(body: ModelRequest, runtime: Annotated[ApiRuntime, Depends(get_runtime)]) -> RuntimeResponse:
        return await runtime.switch_model(body.provider, body.model)

    @router.put("/runtime/thinking")
    async def thinking(body: ThinkingRequest, runtime: Annotated[ApiRuntime, Depends(get_runtime)]) -> RuntimeResponse:
        return await runtime.switch_thinking(body.level)

    return router
