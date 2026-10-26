"""入口无关的模型登录配置：校验、厂商目录路由和 JSON 合并持久化。"""

import asyncio
import json
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, TypeAdapter, ValidationError, ValidationInfo, field_validator

from omt.ai.models_manager import (
    list_provider_models, models as model_providers,
    provider_requires_api_key as provider_requires_api_key, supported_providers as supported_providers,
)

from . import settings
from .settings import AuthCredential, ModelDefinition, NonEmptyString, ProviderModels, Settings


class ProviderConnection(BaseModel):
    """厂商连接；provider 来自注册表，只有要求认证的厂商才必须提供非空 api_key。

    免 Key 厂商会将兼容输入归一为 None，不存储或复用旧占位凭据。
    地址由 ai/models/ 对应厂商模块维护，不再作为登录参数。
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, hide_input_in_errors=True, extra="forbid")
    provider: NonEmptyString
    api_key: SecretStr | None = Field(default=None, repr=False, validate_default=True)

    @field_validator("provider")
    @classmethod
    def _registered_provider(cls, value: str) -> str:
        if value not in supported_providers():
            raise ValueError("模型厂商未注册")
        return value

    @field_validator("api_key")
    @classmethod
    def _nonblank_key(cls, value: SecretStr | None, info: ValidationInfo) -> SecretStr | None:
        provider = info.data.get("provider")
        if not isinstance(provider, str):
            return value
        if not provider_requires_api_key(provider):
            return None
        if value is None or not value.get_secret_value().strip():
            raise ValueError("API Key 不能为空")
        return value


class ModelLogin(ProviderConnection):
    """一次模型配置的完整输入；model_name 是精确 ID，api_key 仅认证厂商需要。"""

    model_name: NonEmptyString


class _PartialSettings(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", hide_input_in_errors=True)
    defaultProvider: NonEmptyString | None = None
    defaultModel: NonEmptyString | None = None
    defaultThinkingLevel: NonEmptyString | None = None

    @field_validator("defaultProvider", "defaultModel", mode="before")
    @classmethod
    def _empty_selection(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value


@dataclass(frozen=True)
class ConfigurationStatus:
    """不含凭据的本地配置检查结果；ready 不代表远程模型可达或 Key 有效。"""

    ready: bool
    message: str


@dataclass(frozen=True)
class ProviderLogin:
    """待选择的完整目录；from_cache 表示查询失败后复用本地目录，connection 不用于展示。"""

    connection: ProviderConnection = field(repr=False)
    model_names: tuple[str, ...]
    from_cache: bool


@dataclass(frozen=True)
class ConfiguredModel:
    """可快速切换的本地模型；有凭据不代表远程 Key 有效或模型可达。"""

    provider: str
    model: str
    available_levels: tuple[str, ...]


def list_configured_models() -> list[ConfiguredModel]:
    """列出已导入模型；需认证厂商要求已存 Key，免 Key 厂商只需目录，不联网、不写入。"""
    documents = _load_documents()
    result: list[ConfiguredModel] = []
    for provider in supported_providers():
        if provider_requires_api_key(provider) and _stored_api_key(documents, provider) is None:
            continue
        catalog = documents.providers.get(provider)
        if catalog is None:
            continue
        for definition in catalog.models:
            if definition.provider not in (None, provider):
                continue
            result.append(ConfiguredModel(provider, definition.id, definition.available_thinking_levels))
    return sorted(result, key=lambda item: (item.provider, item.model))


@dataclass(frozen=True)
class LoginResult:
    """已自动保存的默认模型选择；是否已切换运行实例由入口另行处理。"""

    provider: str
    model_name: str


_DOCUMENT = TypeAdapter(dict[str, JsonValue])
_AUTH = TypeAdapter(dict[str, AuthCredential])
_CATALOG = TypeAdapter(dict[str, ProviderModels])
_MODEL_NAMES = TypeAdapter(list[NonEmptyString], config=ConfigDict(hide_input_in_errors=True))


@dataclass
class _Documents:
    selection: dict[str, JsonValue]
    auth: dict[str, JsonValue]
    catalog: dict[str, JsonValue]
    partial: _PartialSettings
    credentials: dict[str, AuthCredential]
    providers: dict[str, ProviderModels]


def _read_document(path: Path) -> dict[str, JsonValue]:
    try:
        return _DOCUMENT.validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeError):
        raise ValueError(f"配置文件 {path.name} 格式无效，请修复后重试；不会自动覆盖。") from None


def _load_documents() -> _Documents:
    directory = settings.root / "agent"
    selection = _read_document(directory / "settings.json")
    auth = _read_document(directory / "auth.json")
    catalog = _read_document(directory / "model-store.json")
    try:
        return _Documents(selection, auth, catalog, _PartialSettings.model_validate(selection),
                          _AUTH.validate_python(auth), _CATALOG.validate_python(catalog))
    except ValidationError:
        raise ValueError("已有模型配置字段无效，请修复后重试；不会自动覆盖。") from None


def check_model_configuration() -> ConfigurationStatus:
    """检查默认选择、目录及厂商要求的认证；不写文件、不初始化模型、不联网。

    免 Key 厂商不要求 auth.json；认证厂商缺 Key 仍未就绪。损坏配置返回安全提示，
    让入口仍能接收 /login 或退出，但登录保存时会拒绝覆盖损坏文件。
    """
    try:
        documents = _load_documents()
    except (ValueError, OSError):
        return ConfigurationStatus(False, "模型配置无法读取或格式无效，请检查配置文件。")
    provider, model_name = documents.partial.defaultProvider, documents.partial.defaultModel
    if provider is None or model_name is None:
        return ConfigurationStatus(False, "尚未配置默认厂商和模型。")
    if provider not in supported_providers():
        return ConfigurationStatus(False, "默认模型厂商尚未注册。")
    if provider_requires_api_key(provider):
        credential = documents.credentials.get(provider)
        if (credential is None or credential.type != "api_key" or credential.key is None
                or not credential.key.get_secret_value().strip()):
            return ConfigurationStatus(False, "默认模型缺少可用的 API Key。")
    catalog = documents.providers.get(provider)
    definition = next((model for model in catalog.models if model.id == model_name), None) if catalog else None
    if definition is None or definition.provider not in (None, provider):
        return ConfigurationStatus(False, "默认模型尚未导入模型目录。")
    return ConfigurationStatus(True, "默认模型配置已就绪。")


async def discover_models(connection: ProviderConnection) -> list[str]:
    """查询厂商完整目录；地址来自厂商模块，不保存文件或发送聊天请求。"""
    return await asyncio.to_thread(list_provider_models, provider=connection.provider, api_key=connection.api_key)


def _stored_api_key(documents: _Documents, provider: str) -> SecretStr | None:
    if not provider_requires_api_key(provider):
        return None
    credential = documents.credentials.get(provider)
    if credential is None:
        return None
    if credential.type != "api_key":
        raise ValueError("该厂商已有非 API 认证，请人工检查后再登录。")
    key = credential.key
    return key if key is not None and key.get_secret_value().strip() else None


def is_provider_logged_in(provider: str) -> bool:
    """仅表示认证厂商保存了非空 Key；免 Key 厂商返回 False，不伪装登录或服务可达。"""
    return _stored_api_key(_load_documents(), provider) is not None


def _normalize_model_names(model_names: Sequence[str]) -> tuple[str, ...]:
    try:
        names = tuple(sorted(set(_MODEL_NAMES.validate_python(list(model_names)))))
    except ValidationError:
        raise ValueError("厂商模型列表无效") from None
    if not names:
        raise ValueError("厂商模型列表为空")
    return names


async def prepare_provider_login(provider: str, api_key: SecretStr | None = None) -> ProviderLogin:
    """根据厂商能力获取完整列表，尚不落盘；免 Key 厂商省略 api_key 即可。

    每次尝试刷新目录。免 Key 厂商可回退已导入缓存，不保留传入或旧占位 Key。
    认证厂商已有 Key 且未显式替换时，网络失败可回退本地目录；
    新 Key 查询失败则报错，不能把它与旧目录拼接后假称登录成功。
    调用本函数表示用户请求使用该厂商模块定义的地址，不再额外询问是否拉取。
    """
    documents = _load_documents()
    requires_key = provider_requires_api_key(provider)
    key = (api_key if api_key is not None else _stored_api_key(documents, provider)) if requires_key else None
    if requires_key and key is None:
        raise ValueError("该厂商尚未登录，请输入 API Key。")
    connection = ProviderConnection(provider=provider, api_key=key)
    from_cache = False
    try:
        names = _normalize_model_names(await discover_models(connection))
    except Exception:
        cached = documents.providers.get(provider)
        if (requires_key and api_key is not None) or cached is None or not cached.models:
            message = ("无法获取厂商模型列表，请检查网络或 API Key 后重试。" if requires_key else
                       "无法获取本地模型列表，请确认推理服务已启动并已下载模型。")
            raise ValueError(message) from None
        names = _normalize_model_names([model.id for model in cached.models if model.provider in (None, provider)])
        from_cache = True
    return ProviderLogin(connection, names, from_cache)


def _write_document(path: Path, document: dict[str, JsonValue]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            _ = stream.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_model_login(login: ModelLogin, *, model_names: Sequence[str]) -> LoginResult:
    """自动合并完整厂商目录及认证，并把用户选择设为默认模型；不访问网络或终端。

    model_names 为本次取得的全部 ID（也可为离线缓存），选择必须在该列表中。
    地址和已知思考能力来自厂商模块；仅补齐缺失/null 映射，已有映射（含空表）不覆盖。
    同 ID 的额外元数据、其他厂商和未返回旧条目保留，不猜测未登记模型能力。
    认证厂商共用一个 Key；免 Key 厂商不创建或修改 auth.json，保留已有认证原文。
    默认模型会更新，已有思考等级不变；不会在这里重建运行模型。
    JSON 逐文件原子替换，权限 0600；没有跨文件事务/跨进程锁，损坏文件不自动覆盖。
    """
    names = _normalize_model_names(model_names)
    if login.model_name not in names:
        raise ValueError("请选择该厂商列表中的模型")
    documents = _load_documents()
    _ = _stored_api_key(documents, login.provider)
    provider_models = documents.providers.get(login.provider, ProviderModels(models=[])).models
    requires_key = provider_requires_api_key(login.provider)
    if requires_key:
        assert login.api_key is not None
        provider_auth = documents.auth.setdefault(login.provider, {})
        assert isinstance(provider_auth, dict)
        provider_auth.update({"type": "api_key", "key": login.api_key.get_secret_value()})
    provider_data = documents.catalog.setdefault(login.provider, {"models": []})
    assert isinstance(provider_data, dict)
    entries = provider_data["models"]
    assert isinstance(entries, list)
    adapter = model_providers[login.provider]
    base_url = adapter.base_url
    for name in names:
        selected = next((entry for entry in provider_models if entry.id == name), None)
        if selected is not None and selected.provider not in (None, login.provider):
            raise ValueError("已有目录项的模型厂商不一致，请人工检查。")
        mapping = adapter.thinking_level_maps.get(name)
        definition = ModelDefinition(id=name, provider=login.provider, baseUrl=base_url,
                                     thinkingLevelMap=dict(mapping) if mapping is not None else None)
        serialized = definition.model_dump(mode="json", exclude_none=True)
        if selected is None:
            entries.append(serialized)
        else:
            entry = entries[provider_models.index(selected)]
            assert isinstance(entry, dict)
            entry.update({"id": name, "provider": login.provider, "baseUrl": str(base_url)})
            if entry.get("thinkingLevelMap") is None and definition.thinkingLevelMap is not None:
                entry["thinkingLevelMap"] = serialized["thinkingLevelMap"]

    documents.selection.update({"defaultProvider": login.provider, "defaultModel": login.model_name})
    _ = Settings.model_validate(documents.selection)
    directory = settings.root / "agent"
    documents_to_write = [("model-store.json", documents.catalog), ("settings.json", documents.selection)]
    if requires_key:
        documents_to_write.insert(0, ("auth.json", documents.auth))
    try:
        for name, document in documents_to_write:
            path = directory / name
            if not path.exists() or document != _read_document(path):
                _write_document(path, document)
    finally:
        settings.load_settings.cache_clear()
    return LoginResult(login.provider, login.model_name)
