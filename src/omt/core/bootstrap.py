"""CLI 与 HTTP 共用的模型、工具和 Agent 组装；不启动输入循环。"""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.sessions import Connection
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from omt.agent.agent import Agent
from omt.ai.model import Model
from omt.ai.models_manager import models as model_initializers

from . import settings as settings_module
from .configuration import SessionSettings
from .mcp import get_tools
from .prompts import load_system_prompt
from .session_manager import SessionManager
from .settings import Settings, load_auth, load_model_store
from .store import SessionStore
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


@asynccontextmanager
async def open_session_manager(options: SessionSettings) -> AsyncGenerator[SessionManager]:
    """为一个 CLI/API 生命周期组装 Manager，并先清理任务再关闭检查点连接。

    options 的目录必须等于进程 cwd；数据库及 sessions 表必须预建。
    本函数不切换目录、不要求 HTTP 令牌，也不创建终端或监听网络端口。
    """
    if options.workspace_path != Path.cwd().resolve() or not options.workspace_path.is_dir():
        raise ValueError("工作目录必须与启动进程的当前目录一致，请先切换目录再启动")
    if not options.db_path.is_file():
        raise FileNotFoundError("会话数据库不存在，请先创建数据库及 sessions 表")
    store = SessionStore(options.db_path)
    _ = await store.list_sessions(options.workspace_path)
    system_prompt = load_system_prompt()
    model = build_model(settings_module.load_settings())
    roots = options.skill_roots
    if roots is None:
        roots = [settings_module.root / "agent" / "skills", options.workspace_path / "skills"]
    async with AsyncSqliteSaver.from_conn_string(str(options.db_path)) as saver:
        agent = await build_agent(
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
