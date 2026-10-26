"""HTTP 生命周期与配置预览；不持有全局当前会话，不把凭据发回客户端。"""

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from uuid import uuid4

from pydantic import SecretStr

from omt.core import bootstrap, model_configuration as configuration
from omt.core.session_manager import SessionError, SessionManager
from omt.core.store import initialize_session_database

from .schemas import (
    LoginConfirmResponse, LoginPreviewResponse, ModelResponse, OperationError,
    ProviderResponse, RuntimeResponse, ThinkingResponse,
)
from .settings import ApiSettings


RuntimeFactory = Callable[[ApiSettings], AbstractAsyncContextManager[bootstrap.SessionRuntime]]


@dataclass(repr=False)
class _Preview:
    login: configuration.ProviderLogin
    expires_at: float
    timer: asyncio.TimerHandle


class ApiRuntime:
    """在 HTTP 入口协调运行设置、有限的临时登录预览和关闭清理。

    factory 创建可替换的 SessionRuntime；fixed_manager 为嵌入/旧测试提供固定实例，
    此模式不读取模型配置、不支持配置写入和切换。预览只保存在内存，单进程有效。
    """

    def __init__(
        self, options: ApiSettings, factory: RuntimeFactory, *,
        fixed_manager: SessionManager | None = None,
    ) -> None:
        self.options: ApiSettings = options
        self.factory: RuntimeFactory = factory
        self._fixed: SessionManager | None = fixed_manager
        self._runtime: bootstrap.SessionRuntime | None = None
        self._stack: AsyncExitStack = AsyncExitStack()
        self._changing: bool = False
        self._closed: bool = False
        self._message: str = "尚未配置模型"
        self._previews: dict[str, _Preview] = {}
        self._preparing: int = 0
        self._leases: int = 0
        self._operations: set[asyncio.Task[object]] = set()

    async def start(self) -> None:
        """无配置时保持可配置状态；已有配置的初始化失败也不关闭配置入口。"""
        if self.options.workspace_path != Path.cwd().resolve() or not self.options.workspace_path.is_dir():
            raise ValueError("工作目录必须与启动进程的当前目录一致")
        if self._fixed is None:
            status = configuration.check_model_configuration()
            self._message = status.message
            if status.ready:
                error = await self._activate()
                if error is not None:
                    self._message = error.message
        else:
            self._message = "固定 Manager 已就绪，不支持模型配置"

    async def _activate(self) -> OperationError | None:
        if self._runtime is not None:
            return None
        # 只有确认首次配置时才初始化缺失库；正常启动沿用严格预建库检查。
        candidate = AsyncExitStack()
        try:
            runtime = await candidate.enter_async_context(self.factory(self.options))
        except asyncio.CancelledError:
            await candidate.aclose()
            raise
        except Exception:
            await candidate.aclose()
            return OperationError(code="runtime_initialization_failed", message="运行环境初始化失败，请检查配置和会话数据库")
        self._stack = candidate
        self._runtime = runtime
        self._message = "运行环境已就绪"
        return None

    def _manager(self) -> SessionManager | None:
        return self._runtime.manager if self._runtime is not None else self._fixed

    async def require_manager(self) -> SessionManager:
        """每次操作取最新 Manager；切换期间拒绝向旧实例派发新工作。"""
        if self._closed:
            raise SessionError("manager_closed", "服务正在关闭")
        if self._changing:
            raise SessionError("manager_busy", "正在修改运行设置")
        manager = self._manager()
        if manager is None:
            raise SessionError("runtime_not_ready", "请先完成模型配置并启用运行环境")
        return manager

    async def acquire_manager(self) -> SessionManager:
        """请求期间借用当前 Manager，阻止接纳尚未占位时被并行切换交接。"""
        manager = await self.require_manager()
        self._leases += 1
        return manager

    def release_manager(self) -> None:
        self._leases -= 1

    def thinking(self) -> ThinkingResponse | None:
        if self._runtime is None:
            return None
        state = self._runtime.thinking
        return ThinkingResponse(provider=state.provider, model=state.model, level=state.level,
                                reasoning_effort=state.reasoning_effort,
                                available_levels=list(state.available_levels))

    def status(self) -> RuntimeResponse:
        manager = self._manager()
        configured = False if self._fixed is not None else configuration.check_model_configuration().ready
        return RuntimeResponse(
            configured=configured, ready=manager is not None and not self._closed,
            mutable=self._fixed is None, changing=self._changing,
            busy=self._changing or bool(self._leases) or (manager.busy if manager is not None else False),
            running=manager.running if manager is not None else 0,
            thinking=self.thinking(), message=self._message,
        )

    def _ensure_mutable(self) -> None:
        if self._closed:
            raise SessionError("manager_closed", "服务正在关闭")
        if self._fixed is not None:
            raise SessionError("runtime_immutable", "注入的固定 Manager 不支持模型配置")

    def _begin_change(self) -> None:
        self._ensure_mutable()
        manager = self._manager()
        if self._changing or self._leases or (manager is not None and manager.busy):
            raise SessionError("manager_busy", "正在切换设置或处理会话操作")
        self._changing = True

    def providers(self) -> list[ProviderResponse]:
        self._ensure_mutable()
        try:
            return [ProviderResponse(provider=name, logged_in=configuration.is_provider_logged_in(name))
                    for name in configuration.supported_providers()]
        except (ValueError, OSError) as exc:
            raise SessionError("configuration_invalid", "本地模型配置无法读取，请检查配置") from exc

    def models(self) -> list[ModelResponse]:
        self._ensure_mutable()
        current = self.thinking()
        try:
            return [ModelResponse(provider=item.provider, model=item.model,
                                  available_levels=list(item.available_levels),
                                  current=current is not None and
                                  (current.provider, current.model) == (item.provider, item.model))
                    for item in configuration.list_configured_models()]
        except (ValueError, OSError) as exc:
            raise SessionError("configuration_invalid", "本地模型目录无法读取，请检查配置") from exc

    async def switch_model(self, provider: str, model: str) -> RuntimeResponse:
        """仅切换运行模型，必须来自本地快捷列表；不写默认配置。"""
        self._begin_change()
        task = asyncio.current_task()
        if task is not None:
            self._operations.add(task)
        try:
            if self._runtime is None:
                raise SessionError("runtime_not_ready", "请先完成模型配置")
            if not any(item.provider == provider and item.model == model for item in self.models()):
                raise SessionError("invalid_model", "请选择本地已配置的模型")
            await self._runtime.set_model(provider, model)
            self._message = "运行环境已就绪"
            self._changing = False
            return self.status()
        except SessionError:
            raise
        except Exception as exc:
            raise SessionError("model_change_failed", "模型切换失败，原运行配置保持不变") from exc
        finally:
            self._changing = False
            if task is not None:
                self._operations.discard(task)

    async def switch_thinking(self, level: str) -> RuntimeResponse:
        """主动提交的等级必须可选；切模型继承旧等级的回退由 Runtime 保留。"""
        self._begin_change()
        task = asyncio.current_task()
        if task is not None:
            self._operations.add(task)
        try:
            if self._runtime is None:
                raise SessionError("runtime_not_ready", "请先完成模型配置")
            if level not in self._runtime.thinking.available_levels:
                raise SessionError("invalid_thinking_level", "当前模型不支持该思考等级")
            await self._runtime.set_thinking_level(level)
            self._message = "运行环境已就绪"
            self._changing = False
            return self.status()
        except SessionError:
            raise
        except Exception as exc:
            raise SessionError("model_change_failed", "思考等级切换失败，原运行配置保持不变") from exc
        finally:
            self._changing = False
            if task is not None:
                self._operations.discard(task)

    def _discard_preview(self, login_id: str) -> None:
        preview = self._previews.pop(login_id, None)
        if preview is not None:
            preview.timer.cancel()

    def _expire(self) -> None:
        now = monotonic()
        for key, value in list(self._previews.items()):
            if value.expires_at <= now:
                self._discard_preview(key)

    async def preview(self, provider: str, api_key: SecretStr | None) -> LoginPreviewResponse:
        """显式查询注册厂商 provider；api_key=None 复用本地凭据，非空则预览新 Key。

        预览限量且过期；Key 不落盘、不进入响应，失败或取消释放名额。
        """
        self._ensure_mutable()
        self._expire()
        if len(self._previews) + self._preparing >= self.options.login_preview_limit:
            raise SessionError("login_capacity_exceeded", "配置预览数量已达上限，请稍后重试")
        self._preparing += 1
        task = asyncio.current_task()
        if task is not None:
            self._operations.add(task)
        try:
            login = await configuration.prepare_provider_login(provider, api_key)
            login_id = str(uuid4())
            timer = asyncio.get_running_loop().call_later(
                self.options.login_preview_ttl, self._discard_preview, login_id,
            )
            self._previews[login_id] = _Preview(login, monotonic() + self.options.login_preview_ttl, timer)
            return LoginPreviewResponse(login_id=login_id, provider=provider,
                                        models=list(login.model_names), from_cache=login.from_cache,
                                        expires_in=self.options.login_preview_ttl)
        except (ValueError, OSError) as exc:
            raise SessionError("login_preview_failed", "无法获取模型目录，请检查厂商、凭据或本地配置") from exc
        finally:
            self._preparing -= 1
            if task is not None:
                self._operations.discard(task)

    async def confirm(self, login_id: str, model: str) -> LoginConfirmResponse:
        """使用当前进程中未过期的 login_id，确认预览目录内的精确模型 ID model。

        保存与启用不是事务，分别报告结果；成功保存即消耗预览，不能重复确认。
        """
        self._begin_change()
        task = asyncio.current_task()
        if task is not None:
            self._operations.add(task)
        try:
            self._expire()
            preview = self._previews.get(login_id)
            if preview is None:
                raise SessionError("login_preview_not_found", "配置预览不存在或已过期，请重新获取")
            if model not in preview.login.model_names:
                raise SessionError("invalid_model", "请选择本次预览中的模型")
            connection = preview.login.connection
            try:
                _ = configuration.save_model_login(
                    configuration.ModelLogin(provider=connection.provider, api_key=connection.api_key,
                                             model_name=model), model_names=preview.login.model_names,
                )
            except (ValueError, OSError) as exc:
                raise SessionError("configuration_save_failed", "配置保存失败，可能已部分写入，请检查后重新配置") from exc
            self._discard_preview(login_id)
            error: OperationError | None = None
            try:
                if self._runtime is None:
                    initialize_session_database(self.options.db_path)
                    error = await self._activate()
                else:
                    await self._runtime.set_model(connection.provider, model)
            except asyncio.CancelledError:
                raise
            except Exception:
                error = OperationError(code="runtime_initialization_failed",
                                       message="配置已保存，但运行配置未更新；原运行实例保持不变")
            self._message = error.message if error is not None else "运行环境已就绪"
            self._changing = False
            return LoginConfirmResponse(configuration_saved=True, runtime_ready=self._manager() is not None,
                                        runtime=self.status(), error=error)
        finally:
            self._changing = False
            if task is not None:
                self._operations.discard(task)

    async def aclose(self) -> None:
        """先取消配置任务，再关闭 Runtime/Manager/Saver；清除含凭据的内存预览。"""
        self._closed = True
        current = asyncio.current_task()
        tasks = [task for task in self._operations if task is not current]
        for task in tasks:
            _ = task.cancel()
        if tasks:
            _ = await asyncio.gather(*tasks, return_exceptions=True)
        for login_id in list(self._previews):
            self._discard_preview(login_id)
        await self._stack.aclose()
