"""读取 agent/ 下的设置、认证和模型目录；不在这里创建模型或刷新认证。"""

from functools import cache
from pathlib import Path
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from pydantic_settings import BaseSettings

root = Path("~/.omt").expanduser().resolve()
NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Settings(BaseSettings):
    """模型选择配置，对应 agent/settings.json。"""

    defaultProvider: NonEmptyString = Field(description="默认模型提供商")
    defaultModel: NonEmptyString = Field(description="默认模型标识，与模型目录中的 id 一致")
    defaultThinkingLevel: NonEmptyString | None = Field(
        default=None,
        description="可选思考等级；未配置或为 null 时按 none 处理，显式等级按模型 thinkingLevelMap 映射",
    )


class AuthCredential(BaseModel):
    """保留认证类型和 API Key；其他认证字段不会被误作 API Key 或打印。"""

    model_config = ConfigDict(hide_input_in_errors=True)

    type: NonEmptyString
    key: SecretStr | None = None


class ModelDefinition(BaseModel):
    """模型连接与思考映射；其他能力、价格等额外元数据暂不使用。"""

    id: NonEmptyString
    baseUrl: HttpUrl
    provider: NonEmptyString | None = None
    # 等级名映射到提供商的 reasoning_effort；映射缺失、等级缺失或值为 null 时按 none 处理。
    thinkingLevelMap: dict[str, NonEmptyString | None] | None = None

    @property
    def available_thinking_levels(self) -> tuple[str, ...]:
        """统一展示顺序：none 在首位，已声明的 low/medium/high/max 从低到高。

        排除 null 能力，不补造模型未声明的等级；自定义名称无法推断强度，
        放在已知等级后按名称排序。仅派生展示值，不改写映射或配置。
        """
        preferred = ("low", "medium", "high", "max")
        supported = {key for key, value in (self.thinkingLevelMap or {}).items()
                     if key != "none" and value is not None}
        known = tuple(level for level in preferred if level in supported)
        custom = tuple(sorted(supported.difference(preferred)))
        return ("none", *known, *custom)


class ProviderModels(BaseModel):
    """同一提供商下的模型定义。"""

    models: list[ModelDefinition]

    @model_validator(mode="after")
    def _check_unique_model_ids(self) -> Self:
        ids = [model.id for model in self.models]
        if len(ids) != len(set(ids)):
            raise ValueError("同一提供商下的模型 id 不得重复")
        return self


_AUTH_ADAPTER = TypeAdapter(
    dict[str, AuthCredential], config=ConfigDict(hide_input_in_errors=True)
)
_MODEL_STORE_ADAPTER = TypeAdapter(
    dict[str, ProviderModels], config=ConfigDict(hide_input_in_errors=True)
)


@cache
def load_settings() -> Settings:
    """读取并缓存模型选择；保留 cache_clear() 以便调用者主动重载。"""
    settings_path = root / "agent" / "settings.json"
    return Settings.model_validate_json(settings_path.read_text(encoding="utf-8"))


def load_auth() -> dict[str, AuthCredential]:
    """读取最新认证，不缓存密钥，不在错误消息中包含文件原文或凭据。"""
    path = root / "agent" / "auth.json"
    content = path.read_text(encoding="utf-8")
    try:
        return _AUTH_ADAPTER.validate_json(content)
    except ValidationError:
        raise ValueError(
            f"认证配置文件 {path} 格式无效，请检查 JSON 语法和各提供商的 type/key 字段"
        ) from None


def load_model_store() -> dict[str, ProviderModels]:
    """读取最新模型目录，校验 HTTP(S) 地址及同一提供商下的模型 ID 唯一性。"""
    path = root / "agent" / "model-store.json"
    content = path.read_text(encoding="utf-8")
    try:
        return _MODEL_STORE_ADAPTER.validate_json(content)
    except ValidationError:
        raise ValueError(
            f"模型目录 {path} 格式无效，请检查 JSON、models 列表、唯一 id、baseUrl 地址和 thinkingLevelMap"
        ) from None
