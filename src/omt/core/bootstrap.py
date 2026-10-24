"""CLI 与 HTTP 共用的模型、工具和 Agent 组装；不启动输入循环。"""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
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
from .session_manager import SessionError, SessionManager
from .settings import ModelDefinition, Settings, load_auth, load_model_store
from .store import SessionStore
from .tools_manager import load_tools


def build_model(settings: Settings, *, definition: ModelDefinition | None = None) -> Model:
    """解析连接及可选思考等级；未配置或没有有效映射时按 "none" 处理。

    settings 中显式的非 none 等级按所选模型的 thinkingLevelMap 映射后传给提供商。
    未配置、null、字符串 "none"、映射缺失或对应值为 null 时不主动配置思考。
    模型选择与认证仍严格校验，不回退到其他模型或环境密钥。
    definition 可传入已解析的目录快照，以保证展示和初始化使用同一映射；其 ID/provider 必须匹配设置。
    """
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

    definition = definition if definition is not None else _load_model_definition(settings)
    if definition.id != model_id or (definition.provider is not None and definition.provider != provider):
        raise ValueError(f"model-store.json 中模型 {model_id!r} 的模型标识或提供商与配置不一致")
    effort = _thinking_status(settings, definition).reasoning_effort

    return Model(
        provider, model=definition.id, base_url=definition.baseUrl,
        api_key=api_key, reasoning_effort=effort,
    )


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


@dataclass(frozen=True)
class ThinkingStatus:
    """不含凭据的当前模型状态；level 是请求等级，reasoning_effort 是实际映射结果。"""

    provider: str
    model: str
    level: str
    reasoning_effort: str
    available_levels: tuple[str, ...]


def _load_model_definition(settings: Settings) -> ModelDefinition:
    provider_models = load_model_store().get(settings.defaultProvider)
    if provider_models is None:
        raise ValueError(f"model-store.json 中缺少提供商 {settings.defaultProvider!r}")
    definition = next((model for model in provider_models.models if model.id == settings.defaultModel), None)
    if definition is None:
        raise ValueError(
            f"model-store.json 中未找到模型 {settings.defaultModel!r}；"
            + "settings.json 的 defaultModel 必须与 models[].id 完全一致"
        )
    return definition


def _thinking_status(settings: Settings, definition: ModelDefinition) -> ThinkingStatus:
    mapping = definition.thinkingLevelMap or {}
    level = settings.defaultThinkingLevel or "none"
    effort = (mapping.get(level) or "none") if level != "none" else "none"
    levels = tuple(sorted(key for key, value in mapping.items() if key != "none" and value is not None))
    return ThinkingStatus(settings.defaultProvider, settings.defaultModel, level, effort, ("none", *levels))


class SessionRuntime:
    """拥有当前 Manager 的应用生命周期；切换只影响本实例，不改写配置文件。

    由 open_session_runtime 创建，复用工具和 Checkpointer；只有
    set_thinking_level/set_model 成功后才替换 manager。调用者每次操作读取最新 manager，
    不应长期缓存旧实例。关闭资源由外层异步上下文负责。
    """

    def __init__(
        self, manager: SessionManager, settings: Settings, definition: ModelDefinition,
        saver: AsyncSqliteSaver, mcp_tools: Sequence[BaseTool],
    ) -> None:
        """settings/definition 是启动快照，saver 在本实例整个生命周期保持打开。

        mcp_tools 是已发现的 MCP 工具，切换时复用，不重复连接外部服务器。
        """
        self._manager: SessionManager = manager
        self._settings: Settings = settings.model_copy(deep=True)
        self._definition: ModelDefinition = definition.model_copy(deep=True)
        self._saver: AsyncSqliteSaver = saver
        self._mcp_tools: tuple[BaseTool, ...] = tuple(mcp_tools)
        self._closed: bool = False
        self._changing: bool = False

    @property
    def manager(self) -> SessionManager:
        return self._manager

    @property
    def thinking(self) -> ThinkingStatus:
        return _thinking_status(self._settings, self._definition)

    async def set_thinking_level(self, level: str) -> None:
        """从下一轮使用新等级；缺失映射按 none，构建失败保留旧模型和状态。

        level 是目录中的等级名或 none，必须非空。切换会重新创建 Model、Agent、
        Manager，但复用历史和失败保护；不调用模型试运行，不重写设置缓存或文件。
        并行切换立即拒绝；有运行/历史操作时交接也会被 Manager 拒绝。
        """
        self._ensure_can_change()
        candidate = Settings.model_validate({**self._settings.model_dump(), "defaultThinkingLevel": level})
        if (candidate.defaultThinkingLevel or "none") == self.thinking.level:
            return
        await self._replace_model(candidate, self._definition)

    async def set_model(self, provider: str, model_name: str) -> None:
        """选择已导入的模型供后续轮次使用，不读写默认选择或发送试运行请求。

        provider/model_name 为注册厂商及目录精确 ID。本次重新读取目录快照和认证，
        保留当前请求思考等级，缺映射按 none；复用会话、MCP 和检查点。
        构建失败、取消或 Manager 忙碌时保持原实例及状态。
        """
        self._ensure_can_change()
        candidate = Settings.model_validate({**self._settings.model_dump(),
                                             "defaultProvider": provider, "defaultModel": model_name})
        definition = _load_model_definition(candidate)
        await self._replace_model(candidate, definition)

    def _ensure_can_change(self) -> None:
        if self._closed:
            raise SessionError("manager_closed", "服务正在关闭")
        if self._changing:
            raise SessionError("manager_busy", "正在切换模型")

    async def _replace_model(self, candidate: Settings, definition: ModelDefinition) -> None:
        self._changing = True
        try:
            model = build_model(candidate, definition=definition)
            agent = await build_agent(model, checkpointer=self._saver, extra_tools=self._mcp_tools)
            if self._closed:
                raise SessionError("manager_closed", "服务正在关闭")
            successor = await self._manager.rebuild(agent)
            self._manager = successor
            self._settings = candidate
            self._definition = definition
        finally:
            self._changing = False

    async def aclose(self) -> None:
        """先关闭当前 Manager；外层再关闭共享的 SQLite Saver。"""
        self._closed = True
        await self._manager.aclose()


async def _prepare_session(options: SessionSettings) -> tuple[SessionStore, str, list[Path]]:
    """先检查工作目录和预建索引，再读取提示词；不触碰模型认证。"""
    if options.workspace_path != Path.cwd().resolve() or not options.workspace_path.is_dir():
        raise ValueError("工作目录必须与启动进程的当前目录一致，请先切换目录再启动")
    if not options.db_path.is_file():
        raise FileNotFoundError("会话数据库不存在，请先创建数据库及 sessions 表")
    store = SessionStore(options.db_path)
    _ = await store.list_sessions(options.workspace_path)
    system_prompt = load_system_prompt()
    roots = options.skill_roots
    if roots is None:
        roots = [settings_module.root / "agent" / "skills", options.workspace_path / "skills"]
    return store, system_prompt, list(roots)


@asynccontextmanager
async def open_session_runtime(options: SessionSettings) -> AsyncGenerator[SessionRuntime]:
    """为可切换模型的入口持有一个 Saver；切换重建 Manager，不重建数据库连接。"""
    store, prompt, roots = await _prepare_session(options)
    settings = settings_module.load_settings().model_copy(deep=True)
    definition = _load_model_definition(settings)
    model = build_model(settings, definition=definition)
    servers = {name: server.connection() for name, server in options.mcp_servers.items()}
    mcp_tools = await get_tools(servers) if servers else []
    async with AsyncSqliteSaver.from_conn_string(str(options.db_path)) as saver:
        agent = await build_agent(model, checkpointer=saver, extra_tools=mcp_tools)
        manager = SessionManager(
            agent, store, workspace_path=options.workspace_path, system_prompt=prompt,
            skill_roots=roots, max_concurrent_runs=options.max_concurrent_runs,
        )
        runtime = SessionRuntime(manager, settings, definition, saver, mcp_tools)
        try:
            yield runtime
        finally:
            await runtime.aclose()


@asynccontextmanager
async def open_session_manager(options: SessionSettings) -> AsyncGenerator[SessionManager]:
    """为一个 CLI/API 生命周期组装 Manager，并先清理任务再关闭检查点连接。

    options 的目录必须等于进程 cwd；数据库及 sessions 表必须预建。
    本函数不切换目录、不要求 HTTP 令牌，也不创建终端或监听网络端口。
    """
    store, system_prompt, roots = await _prepare_session(options)
    model = build_model(settings_module.load_settings())
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
