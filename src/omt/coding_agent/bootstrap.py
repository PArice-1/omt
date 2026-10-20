"""CLI 与 HTTP 共用的模型、工具和 Agent 组装；不启动输入循环。"""

from collections.abc import Sequence

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.sessions import Connection
from langgraph.checkpoint.base import BaseCheckpointSaver

from omt.agent.agent import Agent
from omt.ai.model import Model
from omt.ai.models_manager import models as model_initializers

from .mcp import get_tools
from .settings import Settings, load_auth, load_model_store
from .tools_manager import load_tools


def build_model(settings: Settings) -> Model:
    """精确解析提供商、认证和模型目录，不回退到其他模型或环境密钥。"""
    provider = settings.defaultProvider
    model_id = settings.defaultModel
    if provider not in model_initializers:
        raise ValueError(f"提供商 {provider!r} 未注册模型初始化函数")

    credential = load_auth().get(provider)
    if credential is None:
        raise ValueError(f"auth.json 中缺少提供商 {provider!r} 的认证配置")
    if credential.type != "api_key":
        raise ValueError(
            f"提供商 {provider!r} 的认证类型 {credential.type!r} 暂不支持；"
            "目前仅支持 api_key，其他认证需要提供商专用适配，不能直接当作 API Key"
        )
    api_key = credential.key
    if api_key is None or not api_key.get_secret_value().strip():
        raise ValueError(f"auth.json 中提供商 {provider!r} 的 API Key 不能为空")

    provider_models = load_model_store().get(provider)
    if provider_models is None:
        raise ValueError(f"model-store.json 中缺少提供商 {provider!r}")
    definition = next((model for model in provider_models.models if model.id == model_id), None)
    if definition is None:
        raise ValueError(
            f"model-store.json 中未找到提供商 {provider!r} 的模型 {model_id!r}；"
            "settings.json 的 defaultModel 必须与 models[].id 完全一致"
        )
    if definition.provider is not None and definition.provider != provider:
        raise ValueError(f"model-store.json 中模型 {model_id!r} 的提供商与配置不一致")

    return Model(provider, model=definition.id, base_url=definition.baseUrl, api_key=api_key)


async def build_agent(
    model: Model,
    *,
    checkpointer: BaseCheckpointSaver[str] | None = None,
    servers: dict[str, Connection] | None = None,
    extra_tools: Sequence[BaseTool] = (),
) -> Agent:
    """显式组装内置工具和可选 MCP；无 MCP 配置时不发现或启动外部服务。"""
    mcp_tools = await get_tools(servers) if servers else []
    return Agent(
        model=model.model,
        tools=[*load_tools(), *extra_tools, *mcp_tools],
        checkpointer=checkpointer,
    )
