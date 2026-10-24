"""注册厂商的模型初始化和目录查询能力；上层不依赖具体厂商模块。"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from langchain_core.language_models import BaseChatModel
from pydantic import HttpUrl, SecretStr, ValidationError

from .models.deepseek import DEEPSEEK_BASE_URL, DEEPSEEK_THINKING_LEVEL_MAPS, deepseek_init, list_deepseek_models


class ModelCatalogLoader(Protocol):
    def __call__(self, *, api_key: SecretStr, base_url: HttpUrl) -> list[str]: ...


@dataclass(frozen=True)
class ModelProvider:
    """厂商适配；base_url 是内置地址，thinking_level_maps 是按精确模型 ID 的已知能力。

    未登记能力的模型不推断等级；默认空表，新厂商可只提供连接/查询能力。
    """

    initialize: Callable[..., BaseChatModel]
    list_models: ModelCatalogLoader
    base_url: HttpUrl
    thinking_level_maps: Mapping[str, Mapping[str, str | None]] = field(
        default_factory=dict[str, Mapping[str, str | None]],
    )


models: dict[str, ModelProvider] = {
    "deepseek": ModelProvider(initialize=deepseek_init, list_models=list_deepseek_models,
                              base_url=DEEPSEEK_BASE_URL, thinking_level_maps=DEEPSEEK_THINKING_LEVEL_MAPS),
}


def supported_providers() -> tuple[str, ...]:
    """返回已注册的厂商名，供入口展示；新增厂商只需补充注册项。"""
    return tuple(sorted(models))


def list_provider_models(*, provider: str, api_key: SecretStr) -> list[str]:
    """按厂商注册地址查询完整列表，上层无需管理连接地址。"""
    adapter = models.get(provider)
    if adapter is None:
        raise ValueError("模型厂商未注册")
    return adapter.list_models(api_key=api_key, base_url=adapter.base_url)


def models_provider(
    provider: str, *, model: str, base_url: HttpUrl, args: dict[str, object],
) -> BaseChatModel:
    """初始化已注册厂商的模型；args 保留 SDK 参数，由厂商函数校验。"""
    adapter = models.get(provider)
    if adapter is None:
        raise ValueError("该模型未找到")
    try:
        return adapter.initialize(provider, model=model, base_url=base_url, **args)
    except ValidationError as exc:
        raise ValueError(f"[参数错误]: {exc}") from exc
