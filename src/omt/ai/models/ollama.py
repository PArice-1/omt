"""将通用模型选项适配为 Ollama 原生本地推理和已下载模型目录。"""

from http.client import HTTPException, HTTPResponse
from typing import IO, ClassVar, override
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, ConfigDict, HttpUrl, SecretStr, validate_call


OLLAMA_BASE_URL = HttpUrl("http://localhost:11434")


class _CatalogModel(BaseModel):
    name: str


class _CatalogResponse(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(hide_input_in_errors=True)
    models: list[_CatalogModel]


class _NoRedirect(HTTPRedirectHandler):
    @override
    def redirect_request(
        self, req: Request, fp: IO[bytes], code: int, msg: str,
        headers: object, newurl: str,
    ) -> None:
        # 本地目录请求不应被重定向到其他推理服务。
        return None


def list_ollama_models(
    *, api_key: SecretStr | None = None, base_url: HttpUrl = OLLAMA_BASE_URL,
) -> list[str]:
    """查询 Ollama /api/tags，返回已下载的完整模型名（保留 :tag）。

    base_url 是原生服务根地址，不是 OpenAI 兼容的 /v1 地址；允许底层显式覆盖
    以连接可信代理或测试服务。api_key 仅兼容厂商目录接口，不使用、不发送。
    查询不下载、加载或运行模型；十秒超时、响应上限 1 MiB，不跟随重定向。
    空目录视为尚未准备好，提示启动服务并下载模型；不根据名称推测思考能力。
    """
    if base_url.username is not None or base_url.password is not None or base_url.query or base_url.fragment:
        raise ValueError("模型地址不得包含认证、查询参数或片段")
    request = Request(
        str(base_url).rstrip("/") + "/api/tags",
        headers={"Accept": "application/json"},
    )
    try:
        response: HTTPResponse = build_opener(_NoRedirect()).open(request, timeout=10)
        with response:
            content = response.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("响应过大")
        catalog = _CatalogResponse.model_validate_json(content)
        names = sorted({model.name.strip() for model in catalog.models if model.name.strip()})
        if not names:
            raise ValueError("模型列表为空")
        return names
    except (URLError, OSError, ValueError, HTTPException) as exc:
        if isinstance(exc, HTTPError):
            exc.close()
        raise ValueError("无法获取 Ollama 本地模型列表，请确认服务已启动并已下载模型。") from None


@validate_call(config=ConfigDict(hide_input_in_errors=True))
def ollama_init(
    provider: str = "ollama",
    *,
    model: str,
    base_url: HttpUrl = OLLAMA_BASE_URL,
    api_key: SecretStr | None = None,
    reasoning_effort: str | None = None,
    **args: object,
) -> BaseChatModel:
    """通过 ChatOllama 初始化原生聊天模型，不请求 OpenAI 兼容接口。

    base_url 为服务根地址，默认 http://localhost:11434。api_key 仅兼容上层接口，
    本地服务不需要该参数，不将它传给 SDK。模型名使用本地完整名称，例如 qwen3:8b。
    reasoning_effort 是目录映射后的值：none/false 关闭，true 开启，low/medium/high
    原样映射到 SDK 的 reasoning；不支持 max，不将 DeepSeek 等级强行套用到本地模型。
    None 不覆盖调用方原生 reasoning 或服务默认行为；显式 effort 优先于原生选项。
    其他原生选项（如 num_ctx、num_predict、temperature、keep_alive）原样保留。
    默认不校验本地模型，构造时不联网；调用方可显式设置 validate_model_on_init=True。
    """
    if not model.strip():
        raise ValueError("Ollama 模型名称不能为空")
    if reasoning_effort in ("none", "false"):
        args["reasoning"] = False
    elif reasoning_effort == "true":
        args["reasoning"] = True
    elif reasoning_effort in ("low", "medium", "high"):
        args["reasoning"] = reasoning_effort
    elif reasoning_effort is not None:
        raise ValueError("Ollama reasoning_effort 仅支持 none、false、true、low、medium、high")
    _ = args.setdefault("validate_model_on_init", False)

    return init_chat_model(
        model_provider=provider,
        model=model,
        base_url=str(base_url),
        configurable_fields=None,
        config_prefix=None,
        **args,
    )
