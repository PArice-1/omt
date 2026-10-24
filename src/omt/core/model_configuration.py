"""入口无关的模型登录配置：校验、厂商目录路由和 JSON 合并持久化。"""

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, JsonValue, SecretStr, TypeAdapter, ValidationError, field_validator

from omt.ai.models_manager import list_provider_models, supported_providers as supported_providers

from . import settings
from .settings import AuthCredential, ModelDefinition, NonEmptyString, ProviderModels, Settings


class ProviderConnection(BaseModel):
    """用户授权的 API 连接；base_url 是凭据发送目标，不默认替换为官网。

    provider 必须来自厂商注册表，目前只有 deepseek。api_key 不能空白；
    base_url 必须为 HTTP(S)，禁止在 URL 中夹带认证、查询参数或片段。
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, hide_input_in_errors=True, extra="forbid")
    provider: NonEmptyString
    api_key: SecretStr = Field(repr=False)
    base_url: HttpUrl

    @field_validator("provider")
    @classmethod
    def _registered_provider(cls, value: str) -> str:
        if value not in supported_providers():
            raise ValueError("模型厂商未注册")
        return value

    @field_validator("api_key")
    @classmethod
    def _nonblank_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("API Key 不能为空")
        return value

    @field_validator("base_url")
    @classmethod
    def _connection_url(cls, value: HttpUrl) -> HttpUrl:
        if value.username is not None or value.password is not None or value.query or value.fragment:
            raise ValueError("模型地址不得包含认证、查询参数或片段")
        return value


class ModelLogin(ProviderConnection):
    """一次 API 登录的完整输入；model_name 是提供商的精确模型 ID，不推断别名。"""

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
class LoginResult:
    """已保存的选择；is_default 表示本次模型与持久化默认选择一致，不代表已切换运行模型。"""

    provider: str
    model_name: str
    is_default: bool


_DOCUMENT = TypeAdapter(dict[str, JsonValue])
_AUTH = TypeAdapter(dict[str, AuthCredential])
_CATALOG = TypeAdapter(dict[str, ProviderModels])


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
    """检查磁盘上的默认模型、API 认证及目录是否齐全；不写文件、不初始化模型、不联网。

    缺文件、缺默认字段、缺模型或缺认证返回未就绪；损坏配置返回安全提示，
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
    """按厂商路由目录查询；仅在用户同意向 base_url 发送 Key 后显式调用。

    目录发现失败不影响手动登录。查询不保存任何文件，不发送聊天请求。
    """
    return await asyncio.to_thread(list_provider_models, provider=connection.provider,
                                   api_key=connection.api_key, base_url=connection.base_url)


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


def save_model_login(login: ModelLogin) -> LoginResult:
    """合并保存 API 登录，不访问网络、数据库或终端，也不切换运行模型。

    login 包含用户明确提交的厂商、模型名、Key 和连接地址。同厂商共用一个 Key，
    重复登录会替换该厂商 Key；同 ID 更新 URL，保留目录思考映射/额外元数据。
    仅补齐缺失/null/空白的默认选择，不覆盖已有默认模型及思考等级。
    JSON 逐文件以 0600 临时文件原子替换，不提供跨文件事务或跨进程锁；
    损坏文件和已有非 API 认证拒绝覆盖。成功或部分 I/O 失败都会清除设置缓存。
    """
    documents = _load_documents()
    partial = documents.partial
    if partial.defaultModel is None and partial.defaultProvider not in (None, login.provider):
        raise ValueError("默认厂商尚缺模型，请先登录该厂商以补齐默认选择。")
    credential = documents.credentials.get(login.provider)
    if credential is not None and credential.type != "api_key":
        raise ValueError("该厂商已有非 API 认证，请人工检查后再登录。")
    provider_models = documents.providers.get(login.provider, ProviderModels(models=[])).models
    selected = next((entry for entry in provider_models if entry.id == login.model_name), None)
    if selected is not None and selected.provider not in (None, login.provider):
        raise ValueError("已有目录项的模型厂商不一致，请人工检查。")

    provider_auth = documents.auth.setdefault(login.provider, {})
    assert isinstance(provider_auth, dict)
    provider_auth.update({"type": "api_key", "key": login.api_key.get_secret_value()})
    provider_data = documents.catalog.setdefault(login.provider, {"models": []})
    assert isinstance(provider_data, dict)
    entries = provider_data["models"]
    assert isinstance(entries, list)
    definition = ModelDefinition(id=login.model_name, provider=login.provider, baseUrl=login.base_url)
    if selected is None:
        entries.append(definition.model_dump(mode="json", exclude_none=True))
    else:
        entry = entries[provider_models.index(selected)]
        assert isinstance(entry, dict)
        entry.update({"id": login.model_name, "provider": login.provider, "baseUrl": str(login.base_url)})

    if partial.defaultProvider is None:
        documents.selection["defaultProvider"] = login.provider
    if partial.defaultModel is None:
        documents.selection["defaultModel"] = login.model_name
    default = Settings.model_validate(documents.selection)
    directory = settings.root / "agent"
    try:
        for name, document in (("auth.json", documents.auth), ("model-store.json", documents.catalog),
                               ("settings.json", documents.selection)):
            path = directory / name
            if not path.exists() or document != _read_document(path):
                _write_document(path, document)
    finally:
        settings.load_settings.cache_clear()
    return LoginResult(login.provider, login.model_name,
                       default.defaultProvider == login.provider and default.defaultModel == login.model_name)
