"""core 登录、完整目录导入和默认选择回归；网络、凭据与用户目录完全隔离。"""

import asyncio
import json
import os
import sqlite3
import traceback
from collections.abc import Iterator, Sequence
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import HttpUrl, SecretStr, ValidationError

from omt.ai import models_manager
from omt.ai.models.deepseek import DEEPSEEK_BASE_URL
from omt.core import model_configuration as configuration, settings
from omt.core.model_configuration import ModelLogin, ProviderConnection, check_model_configuration, save_model_login
from omt.core.store import SessionStore, initialize_session_database


@pytest.fixture(autouse=True)
def isolated_login(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in ("defaultProvider", "defaultModel", "defaultThinkingLevel"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("omt.ai.models.deepseek.build_opener", Mock(side_effect=AssertionError("禁止真实 HTTP")))
    yield


def save(model_name: str = "flash", *, provider: str = "deepseek", model_names: Sequence[str] = ("flash", "pro")) -> configuration.LoginResult:
    return save_model_login(ModelLogin(provider=provider, model_name=model_name, api_key=SecretStr("fake-login-key")),
                            model_names=model_names)


def document(name: str, value: object) -> Path:
    directory = settings.root / "agent"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    _ = path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_ollama_first_configuration_needs_no_key_or_auth_file(monkeypatch: pytest.MonkeyPatch) -> None:
    discover = AsyncMock(return_value=["qwen3:8b", "llama3.2:latest"])
    monkeypatch.setattr(configuration, "discover_models", discover)
    prepared = asyncio.run(configuration.prepare_provider_login("ollama"))
    assert prepared.connection.api_key is None and not prepared.from_cache
    assert not (settings.root / "agent").exists()
    result = save_model_login(ModelLogin(provider="ollama", model_name="qwen3:8b"),
                              model_names=prepared.model_names)
    assert result.provider == "ollama" and check_model_configuration().ready
    assert settings.load_settings().defaultModel == "qwen3:8b"
    assert {path.name for path in (settings.root / "agent").iterdir()} == {"settings.json", "model-store.json"}
    assert [(item.provider, item.model) for item in configuration.list_configured_models()] == [
        ("ollama", "llama3.2:latest"), ("ollama", "qwen3:8b"),
    ]


@pytest.mark.parametrize("key", [None, "", " ", "test-unused-key"])
def test_ollama_ignores_keys_and_preserves_existing_auth(key: str | None) -> None:
    path = document("auth.json", {
        "deepseek": {"type": "api_key", "key": "test-deepseek-key"},
        "ollama": {"type": "oauth", "access": "test-old-unused-token"},
    })
    before = path.read_bytes()
    login = ModelLogin(provider="ollama", model_name="local", api_key=SecretStr(key) if key is not None else None)
    assert login.api_key is None
    _ = save_model_login(login, model_names=["local"])
    assert path.read_bytes() == before
    assert check_model_configuration().ready
    assert not configuration.is_provider_logged_in("ollama")
    assert configuration.is_provider_logged_in("deepseek")


@pytest.mark.parametrize("cached", [False, True])
def test_ollama_discovery_failure_needs_no_key_and_only_uses_real_cache(
    cached: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if cached:
        _ = save_model_login(ModelLogin(provider="ollama", model_name="local"), model_names=["local"])
    discover = AsyncMock(side_effect=ValueError("test-private-marker"))
    monkeypatch.setattr(configuration, "discover_models", discover)
    if cached:
        prepared = asyncio.run(configuration.prepare_provider_login("ollama", SecretStr("ignored-key")))
        assert prepared.from_cache and prepared.model_names == ("local",)
        assert prepared.connection.api_key is None
    else:
        with pytest.raises(ValueError, match="推理服务已启动") as raised:
            _ = asyncio.run(configuration.prepare_provider_login("ollama"))
        assert "test-private-marker" not in str(raised.value) and "API Key" not in str(raised.value)
        assert not (settings.root / "agent").exists()
    assert not (settings.root / "agent" / "auth.json").exists()


def test_first_login_saves_entire_catalog_and_provider_url_without_database(tmp_path: Path) -> None:
    assert not check_model_configuration().ready
    assert not configuration.is_provider_logged_in("deepseek")
    assert not (settings.root / "agent").exists()
    result = save()
    assert result.model_name == "flash"
    assert settings.load_settings().defaultModel == "flash"
    credential = settings.load_auth()["deepseek"].key
    assert credential is not None and credential.get_secret_value() == "fake-login-key"
    models = settings.load_model_store()["deepseek"].models
    assert [entry.id for entry in models] == ["flash", "pro"]
    assert all(entry.baseUrl == DEEPSEEK_BASE_URL and entry.thinkingLevelMap is None for entry in models)
    assert check_model_configuration().ready
    assert configuration.is_provider_logged_in("deepseek")
    assert not (tmp_path / "agent" / "state").exists()
    if os.name == "posix":
        assert (settings.root / "agent" / "auth.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("existing", [False, True])
def test_login_imports_known_thinking_capabilities_and_repairs_old_entries(existing: bool) -> None:
    if existing:
        _ = document("model-store.json", {"deepseek": {"models": [{
            "id": "deepseek-flash", "baseUrl": str(DEEPSEEK_BASE_URL), "price": {"input": 1},
        }]}})
    names = ("deepseek-flash", "deepseek-v4-pro", "deepseek-flash-unknown")
    _ = save("deepseek-flash", model_names=names)
    models = {model.id: model for model in settings.load_model_store()["deepseek"].models}
    expected = {"low": "low", "high": "high", "max": "max"}
    assert models["deepseek-flash"].thinkingLevelMap == expected
    assert models["deepseek-v4-pro"].thinkingLevelMap == expected
    assert models["deepseek-flash-unknown"].thinkingLevelMap is None
    path = settings.root / "agent" / "model-store.json"
    if existing:
        assert json.loads(path.read_text())["deepseek"]["models"][0]["price"] == {"input": 1}
    before = path.read_bytes()
    _ = save("deepseek-flash", model_names=names)
    assert path.read_bytes() == before


@pytest.mark.parametrize("mapping", [None, {}, {"high": "custom"}, {"high": None}])
def test_known_capabilities_only_fill_missing_or_null_mapping(mapping: dict[str, str | None] | None) -> None:
    _ = document("model-store.json", {"deepseek": {"models": [{
        "id": "deepseek-flash", "baseUrl": str(DEEPSEEK_BASE_URL), "thinkingLevelMap": mapping,
    }]}})
    _ = save("deepseek-flash", model_names=("deepseek-flash",))
    actual = settings.load_model_store()["deepseek"].models[0].thinkingLevelMap
    assert actual == ({"low": "low", "high": "high", "max": "max"} if mapping is None else mapping)


@pytest.mark.parametrize("partial", [{}, {"defaultProvider": "deepseek"}, {"defaultModel": "old-model"},
                                    {"defaultProvider": None, "defaultModel": "  "}])
def test_selection_fills_or_replaces_default_fields(partial: dict[str, object]) -> None:
    _ = document("settings.json", partial)
    _ = save("pro")
    assert settings.load_settings().defaultModel == "pro"
    assert settings.load_settings().defaultProvider == "deepseek"
    assert check_model_configuration().ready


def test_selection_updates_default_but_preserves_metadata_other_providers_and_auth() -> None:
    _ = save()
    _ = document("settings.json", {"defaultProvider": "deepseek", "defaultModel": "flash",
                                   "defaultThinkingLevel": "high"})
    _ = document("model-store.json", {"deepseek": {"source": "keep", "models": [{
        "id": "flash", "baseUrl": "https://old.test/v1", "thinkingLevelMap": {"high": "max"}, "cost": {"input": 1},
    }, {"id": "legacy", "baseUrl": "https://legacy.test"}]}, "other": {"models": []}})
    auth = document("auth.json", {"deepseek": {"type": "api_key", "key": "fake-login-key", "label": "keep"},
                                  "other": {"type": "oauth", "access": "fake-token"}})
    before_auth = auth.read_bytes()
    _ = settings.load_settings()
    _ = save("pro")
    assert settings.load_settings().defaultModel == "pro"
    assert settings.load_settings().defaultThinkingLevel == "high"
    models = settings.load_model_store()["deepseek"].models
    assert [entry.id for entry in models] == ["flash", "legacy", "pro"]
    assert models[0].baseUrl == DEEPSEEK_BASE_URL
    assert models[0].thinkingLevelMap == {"high": "max"}
    assert str(models[1].baseUrl) == "https://legacy.test/"
    assert '"cost"' in (auth.parent / "model-store.json").read_text()
    assert '"source": "keep"' in (auth.parent / "model-store.json").read_text()
    assert auth.read_bytes() == before_auth
    assert settings.load_auth()["other"].type == "oauth"


def test_repeated_import_deduplicates_full_catalog_and_selected_model() -> None:
    _ = save(model_names=(" pro ", "flash", "flash"))
    _ = save("pro")
    assert [entry.id for entry in settings.load_model_store()["deepseek"].models] == ["flash", "pro"]
    assert settings.load_settings().defaultModel == "pro"


@pytest.mark.parametrize("missing", ["auth.json", "settings.json", "model-store.json"])
def test_login_repairs_missing_file_and_invalidates_cached_settings(missing: str) -> None:
    _ = save()
    _ = settings.load_settings()
    (settings.root / "agent" / missing).unlink()
    assert not check_model_configuration().ready
    _ = save("pro")
    assert check_model_configuration().ready
    assert settings.load_settings().defaultModel == "pro"


def test_prepare_reuses_key_fetches_all_models_and_does_not_write(monkeypatch: pytest.MonkeyPatch) -> None:
    _ = save()
    directory = settings.root / "agent"
    before = {path: path.read_bytes() for path in directory.iterdir()}
    discover = AsyncMock(return_value=["pro", "flash", "new-model", "flash"])
    monkeypatch.setattr(configuration, "discover_models", discover)
    prepared = asyncio.run(configuration.prepare_provider_login("deepseek"))
    assert prepared.model_names == ("flash", "new-model", "pro")
    assert not prepared.from_cache
    connection = discover.call_args.args[0]
    assert isinstance(connection, ProviderConnection)
    assert connection.api_key is not None and connection.api_key.get_secret_value() == "fake-login-key"
    assert {path: path.read_bytes() for path in directory.iterdir()} == before


def test_prepare_requires_key_for_first_login_without_writing() -> None:
    with pytest.raises(ValueError, match="尚未登录"):
        _ = asyncio.run(configuration.prepare_provider_login("deepseek"))
    assert not (settings.root / "agent").exists()


@pytest.mark.parametrize("has_cache", [False, True])
def test_prepare_network_failure_uses_cache_only_for_saved_key(
    monkeypatch: pytest.MonkeyPatch, has_cache: bool,
) -> None:
    if has_cache:
        _ = save()
    monkeypatch.setattr(configuration, "discover_models", AsyncMock(side_effect=ValueError("fake-secret-marker")))
    if has_cache:
        prepared = asyncio.run(configuration.prepare_provider_login("deepseek"))
        assert prepared.from_cache and prepared.model_names == ("flash", "pro")
    with pytest.raises(ValueError) as raised:
        _ = asyncio.run(configuration.prepare_provider_login("deepseek", SecretStr("fake-new-key")))
    assert "fake-secret-marker" not in str(raised.value)
    if not has_cache:
        assert not (settings.root / "agent").exists()


@pytest.mark.parametrize("model_names", [(), (" ",), ("different",)])
def test_empty_invalid_or_unselectable_catalog_is_not_saved(model_names: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        _ = save(model_names=model_names)
    assert not (settings.root / "agent").exists()


@pytest.mark.parametrize("bad", ["not-json fake-secret-marker", '{"deepseek":{"type":123,"key":"fake-secret-marker"}}'])
def test_invalid_existing_auth_is_rejected_without_overwrite_or_secret_in_traceback(bad: str) -> None:
    path = document("auth.json", {})
    _ = path.write_text(bad)
    assert not check_model_configuration().ready
    with pytest.raises(ValueError) as raised:
        _ = save()
    assert "fake-secret-marker" not in "".join(traceback.format_exception(raised.value))
    assert path.read_text() == bad
    assert not (path.parent / "settings.json").exists()


@pytest.mark.parametrize("change", [{"provider": "unknown"}, {"model_name": " "}, {"api_key": " "},
                                    {"base_url": "https://example.test"}])
def test_login_input_validation_hides_secrets_and_no_longer_accepts_url(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as raised:
        _ = ModelLogin.model_validate({"provider": "deepseek", "model_name": "flash", "api_key": "fake-secret", **change})
    assert "fake-secret" not in str(raised.value)
    assert not (settings.root / "agent").exists()


@pytest.mark.parametrize("requires_key", [True, False])
def test_new_provider_defines_its_own_address_without_cli_or_core_changes(
    monkeypatch: pytest.MonkeyPatch, requires_key: bool,
) -> None:
    received: list[tuple[str | None, str]] = []

    def catalog(*, api_key: SecretStr | None, base_url: HttpUrl) -> list[str]:
        received.append((api_key.get_secret_value() if api_key is not None else None, str(base_url)))
        return ["second-flash", "second-pro"]

    initializer = Mock(side_effect=AssertionError("配置阶段不初始化聊天模型"))
    monkeypatch.setitem(models_manager.models, "second", models_manager.ModelProvider(
        initialize=initializer, list_models=catalog, base_url=HttpUrl("https://second.test/v1"),
        thinking_level_maps={"second-pro": {"high": "max"}}, requires_api_key=requires_key,
    ))
    prepared = asyncio.run(configuration.prepare_provider_login("second", SecretStr("fake-login-key")))
    assert prepared.model_names == ("second-flash", "second-pro")
    _ = save("second-pro", provider="second", model_names=prepared.model_names)
    assert settings.load_settings().defaultProvider == "second"
    assert check_model_configuration().ready
    assert all(str(model.baseUrl) == "https://second.test/v1" for model in settings.load_model_store()["second"].models)
    assert settings.load_model_store()["second"].models[1].thinkingLevelMap == {"high": "max"}
    assert received == [("fake-login-key" if requires_key else None, "https://second.test/v1")]
    assert (settings.root / "agent" / "auth.json").exists() is requires_key
    initializer.assert_not_called()


@pytest.mark.parametrize("case", ["oauth", "mismatched-model"])
def test_incompatible_existing_configuration_is_not_overwritten(case: str) -> None:
    if case == "oauth":
        _ = document("auth.json", {"deepseek": {"type": "oauth", "access": "fake-token"}})
    else:
        _ = document("model-store.json", {"deepseek": {"models": [{
            "id": "flash", "provider": "other", "baseUrl": "https://example.test",
        }]}})
    before = {path: path.read_bytes() for path in (settings.root / "agent").iterdir()}
    with pytest.raises(ValueError):
        _ = save()
    assert {path: path.read_bytes() for path in (settings.root / "agent").iterdir()} == before


@pytest.mark.parametrize("existing", [False, True])
def test_explicit_database_initialization_only_creates_missing_files(tmp_path: Path, existing: bool) -> None:
    path = tmp_path / "state" / "test.sqlite"
    if existing:
        path.parent.mkdir()
        with closing(sqlite3.connect(path)) as connection:
            _ = connection.execute("CREATE TABLE custom (value TEXT)")
            _ = connection.execute("INSERT INTO custom VALUES ('keep')")
            connection.commit()
        before = path.read_bytes()
        initialize_session_database(path)
        assert path.read_bytes() == before
    else:
        initialize_session_database(path)
        assert asyncio.run(SessionStore(path).list_sessions(tmp_path)) == []
