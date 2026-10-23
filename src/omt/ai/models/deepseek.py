"""将通用模型选项适配为 DeepSeek 的初始化参数。"""

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from pydantic import HttpUrl, SecretStr, validate_call


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

    reasoning_effort 是目录映射后的提供商值；未传、None 或字符串 "none" 时不主动配置思考。
    其他值原样传给 SDK，并按 DeepSeek 协议在 extra_body 中启用 thinking。
    extra_body 是额外请求体；启用思考时覆盖其 thinking 配置，保留其他字段，不修改调用方字典。
    """
    key = api_key.get_secret_value()
    if not key.strip():
        raise ValueError(f"{model} 的 API_KEY 不能为空")

    if reasoning_effort is not None and reasoning_effort != "none":
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
