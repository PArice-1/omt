"""将通用模型选项适配为 DeepSeek 的初始化参数。"""

from http.client import HTTPException, HTTPResponse
from typing import IO, ClassVar, override
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, ConfigDict, HttpUrl, SecretStr, validate_call


DEEPSEEK_BASE_URL = HttpUrl("https://api.deepseek.com")
# /models 只返回 ID；这里维护官方已确认的能力，按精确 ID 匹配，不猜测未知模型。
# 协议来源：https://api-docs.deepseek.com/guides/thinking_mode/
DEEPSEEK_THINKING_LEVEL_MAPS: dict[str, dict[str, str | None]] = {
    model: {"low": "low", "high": "high", "max": "max"}
    for model in ("deepseek-flash", "deepseek-v4-pro")
}


class _CatalogModel(BaseModel):
    id: str


class _CatalogResponse(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(hide_input_in_errors=True)
    data: list[_CatalogModel]


class _NoRedirect(HTTPRedirectHandler):
    @override
    def redirect_request(
        self, req: Request, fp: IO[bytes], code: int, msg: str,
        headers: object, newurl: str,
    ) -> None:
        # 不允许重定向把 Key 发送到未经用户确认的目标。
        return None


def list_deepseek_models(*, api_key: SecretStr, base_url: HttpUrl = DEEPSEEK_BASE_URL) -> list[str]:
    """查询 DeepSeek 的完整模型 ID 列表，不初始化聊天模型。

    默认地址由本厂商模块维护，/login 无需输入。base_url 仅供底层显式覆盖（如测试
    或代理适配），在其路径后追加 /models；不跟随重定向改变凭据发送目标。
    十秒超时、响应上限 1 MiB、不跟随重定向；只返回 ID，不猜测思考能力。
    """
    if not api_key.get_secret_value().strip():
        raise ValueError("API Key 不能为空")
    if base_url.username is not None or base_url.password is not None or base_url.query or base_url.fragment:
        raise ValueError("模型地址不得包含认证、查询参数或片段")
    request = Request(
        str(base_url).rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {api_key.get_secret_value()}", "Accept": "application/json"},
    )
    try:
        response: HTTPResponse = build_opener(_NoRedirect()).open(request, timeout=10)
        with response:
            content = response.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("响应过大")
        catalog = _CatalogResponse.model_validate_json(content)
        ids = sorted({model.id.strip() for model in catalog.data if model.id.strip()})
        if not ids:
            raise ValueError("模型列表为空")
        return ids
    except (URLError, OSError, ValueError, HTTPException) as exc:
        if isinstance(exc, HTTPError):
            exc.close()
        raise ValueError("无法获取模型列表，请检查连接和 API Key，或手动输入模型名称。") from None


@validate_call
def deepseek_init(
    provider: str,
    *,
    model: str,
    base_url: HttpUrl,
    api_key: SecretStr,
    reasoning_effort: str | None = None,
    extra_body: dict[str, object] | None = None,
    **args: object,
) -> BaseChatModel:
    """初始化 DeepSeek 模型，保留其他 SDK 选项。

    reasoning_effort 是目录映射后的提供商值；未传或 None 时沿用服务端配置。
    已知支持关闭推理的模型收到 "none" 时显式关闭；未知/旧模型保留省略参数的兼容行为。
    其他值原样传给 SDK，并按 DeepSeek 协议在 extra_body 中启用 thinking。
    extra_body 是额外请求体；启用思考时覆盖其 thinking 配置，保留其他字段，不修改调用方字典。
    """
    key = api_key.get_secret_value()
    if not key.strip():
        raise ValueError(f"{model} 的 API_KEY 不能为空")

    if reasoning_effort == "none" and model in DEEPSEEK_THINKING_LEVEL_MAPS:
        args["reasoning_effort"] = "none"
        extra_body = {**(extra_body or {}), "thinking": {"type": "disabled"}}
    elif reasoning_effort is not None and reasoning_effort != "none":
        args["reasoning_effort"] = reasoning_effort
        extra_body = {**(extra_body or {}), "thinking": {"type": "enabled"}}
    if extra_body is not None:
        args["extra_body"] = extra_body

    return init_chat_model(
        model_provider=provider,
        model=model,
        api_key=api_key,
        base_url=str(base_url),
        # OMT 需要可直接调用的聊天模型，而非运行时再选模型的配置包装器。
        configurable_fields=None,
        config_prefix=None,
        **args,
    )
