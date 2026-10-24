"""注册厂商的模型初始化和目录查询能力；上层不依赖具体厂商模块。"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from langchain_core.language_models import BaseChatModel
from pydantic import HttpUrl, SecretStr, ValidationError

from .models.deepseek import deepseek_init, list_deepseek_models


class ModelCatalogLoader(Protocol):
    def __call__(self, *, api_key: SecretStr, base_url: HttpUrl) -> list[str]: ...


@dataclass(frozen=True)
class ModelProvider:
    """厂商适配：initialize 接收 SDK 选项，list_models 根据用户连接查询模型 ID。"""

    initialize: Callable[..., BaseChatModel]
    list_models: ModelCatalogLoader


models: dict[str, ModelProvider] = {
    "deepseek": ModelProvider(initialize=deepseek_init, list_models=list_deepseek_models),
}


def supported_providers() -> tuple[str, ...]:
    """返回已注册的厂商名，供入口展示；新增厂商只需补充注册项。"""
    return tuple(sorted(models))


def list_provider_models(*, provider: str, api_key: SecretStr, base_url: HttpUrl) -> list[str]:
    """路由目录查询；base_url 是用户授权发送 Key 的地址，不自动回退到官网。"""
    adapter = models.get(provider)
    if adapter is None:
        raise ValueError("模型厂商未注册")
    return adapter.list_models(api_key=api_key, base_url=base_url)


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
