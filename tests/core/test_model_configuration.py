"""通过 core 的公开登录接口验证配置持久化；模型和网络完全隔离。"""

import asyncio
import json
import os
import sqlite3
import traceback
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import HttpUrl, SecretStr, ValidationError

from omt.ai import models_manager
from omt.core import model_configuration as configuration, settings
from omt.core.model_configuration import ModelLogin, ProviderConnection, check_model_configuration, save_model_login
from omt.core.store import SessionStore, initialize_session_database


@pytest.fixture(autouse=True)
def clear_selection_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in ("defaultProvider", "defaultModel", "defaultThinkingLevel"):
        monkeypatch.delenv(key, raising=False)
    yield


def login_request(*, model_name: str = "first-model", provider: str = "deepseek") -> ModelLogin:
    return ModelLogin(provider=provider, model_name=model_name, api_key=SecretStr("fake-login-key"),
                      base_url=HttpUrl("https://example.test/custom/v1"))


def document(name: str, value: object) -> Path:
    directory = settings.root / "agent"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    _ = path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_first_login_creates_three_files_without_network_or_database(tmp_path: Path) -> None:
    assert not check_model_configuration().ready
    assert not (settings.root / "agent").exists()
    result = save_model_login(login_request())
    assert result.is_default
    assert settings.load_settings().defaultProvider == "deepseek"
    assert settings.load_settings().defaultModel == "first-model"
    credential = settings.load_auth()["deepseek"].key
    assert credential is not None and credential.get_secret_value() == "fake-login-key"
    models = settings.load_model_store()["deepseek"].models
    assert [entry.id for entry in models] == ["first-model"]
    assert str(models[0].baseUrl) == "https://example.test/custom/v1"
    assert models[0].thinkingLevelMap is None
    assert check_model_configuration().ready
    assert not (tmp_path / "agent" / "state").exists()
    if os.name == "posix":
        assert (settings.root / "agent" / "auth.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("partial", [{}, {"defaultProvider": "deepseek"}, {"defaultModel": "first-model"},
                                    {"defaultProvider": None, "defaultModel": "  "}])
def test_login_fills_missing_default_fields(partial: dict[str, object]) -> None:
    _ = document("settings.json", partial)
    assert not check_model_configuration().ready
    _ = save_model_login(login_request())
    assert settings.load_settings().defaultModel == "first-model"
    assert settings.load_settings().defaultProvider == "deepseek"
    assert check_model_configuration().ready


def test_additional_model_preserves_default_and_existing_metadata_and_updates_provider_key() -> None:
    _ = save_model_login(login_request())
    selection = document("settings.json", {"defaultProvider": "deepseek", "defaultModel": "first-model",
                                           "defaultThinkingLevel": "high"})
    _ = document("model-store.json", {"deepseek": {"source": "keep", "models": [{
        "id": "first-model", "baseUrl": "https://old.test/v1", "thinkingLevelMap": {"high": "max"},
        "cost": {"input": 1},
    }]}, "other": {"models": []}})
    _ = document("auth.json", {"deepseek": {"type": "api_key", "key": "old-fake-key", "label": "keep"},
                               "other": {"type": "oauth", "access": "fake-token"}})
    before = selection.read_bytes()
    _ = settings.load_settings()
    result = save_model_login(login_request(model_name="second-model"))
    assert not result.is_default
    assert selection.read_bytes() == before
    assert settings.load_settings().defaultThinkingLevel == "high"
    catalog = settings.load_model_store()
    assert [entry.id for entry in catalog["deepseek"].models] == ["first-model", "second-model"]
    assert str(catalog["deepseek"].models[0].baseUrl) == "https://old.test/v1"
    assert catalog["deepseek"].models[0].thinkingLevelMap == {"high": "max"}
    assert '"source": "keep"' in (selection.parent / "model-store.json").read_text()
    assert '"cost"' in (selection.parent / "model-store.json").read_text()
    assert '"label": "keep"' in (selection.parent / "auth.json").read_text()
    assert settings.load_auth()["other"].type == "oauth"
    credential = settings.load_auth()["deepseek"].key
    assert credential is not None and credential.get_secret_value() == "fake-login-key"


def test_repeated_login_updates_url_without_duplicate_or_losing_thinking_mapping() -> None:
    _ = document("model-store.json", {"deepseek": {"models": [{
        "id": "first-model", "baseUrl": "https://old.test", "thinkingLevelMap": {"high": "max"},
    }]}})
    _ = save_model_login(login_request())
    models = settings.load_model_store()["deepseek"].models
    assert len(models) == 1
    assert str(models[0].baseUrl) == "https://example.test/custom/v1"
    assert models[0].thinkingLevelMap == {"high": "max"}


@pytest.mark.parametrize("missing", ["auth.json", "settings.json", "model-store.json"])
def test_login_repairs_missing_file_and_invalidates_cached_settings(missing: str) -> None:
    _ = save_model_login(login_request())
    _ = settings.load_settings()
    (settings.root / "agent" / missing).unlink()
    assert not check_model_configuration().ready
    _ = save_model_login(login_request())
    assert check_model_configuration().ready
    assert settings.load_settings().defaultModel == "first-model"


@pytest.mark.parametrize("bad", ["not-json fake-secret-marker", '{"deepseek":{"type":123,"key":"fake-secret-marker"}}'])
def test_invalid_existing_auth_is_rejected_without_overwrite_or_secret_in_traceback(bad: str) -> None:
    path = document("auth.json", {})
    _ = path.write_text(bad)
    assert not check_model_configuration().ready
    with pytest.raises(ValueError) as raised:
        _ = save_model_login(login_request())
    assert "fake-secret-marker" not in "".join(traceback.format_exception(raised.value))
    assert path.read_text() == bad
    assert not (path.parent / "settings.json").exists()


@pytest.mark.parametrize("change", [
    {"provider": "unknown"}, {"model_name": " "}, {"api_key": " "},
    {"base_url": "file:///tmp/x"}, {"base_url": "https://fake-secret@example.test"},
    {"base_url": "https://example.test?key=fake-secret"},
])
def test_login_input_validation_hides_sensitive_values_and_has_no_side_effects(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as raised:
        _ = ModelLogin.model_validate({"provider": "deepseek", "model_name": "first-model",
                                      "api_key": "fake-secret", "base_url": "https://example.test", **change})
    assert "fake-secret" not in str(raised.value)
    assert not (settings.root / "agent").exists()


def test_new_provider_routes_catalog_and_persistence_without_core_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[tuple[str, str]] = []

    def catalog(*, api_key: SecretStr, base_url: HttpUrl) -> list[str]:
        received.append((api_key.get_secret_value(), str(base_url)))
        return ["new-provider-model"]

    initializer = Mock(side_effect=AssertionError("配置阶段不初始化聊天模型"))
    monkeypatch.setitem(models_manager.models, "second", models_manager.ModelProvider(
        initialize=initializer, list_models=catalog,
    ))
    assert configuration.supported_providers() == ("deepseek", "second")
    connection = ProviderConnection(provider="second", api_key=SecretStr("fake-login-key"),
                                    base_url=HttpUrl("https://example.test/custom/v1"))
    assert asyncio.run(configuration.discover_models(connection)) == ["new-provider-model"]
    _ = save_model_login(login_request(provider="second", model_name="new-provider-model"))
    assert settings.load_settings().defaultProvider == "second"
    assert check_model_configuration().ready
    assert received == [("fake-login-key", "https://example.test/custom/v1")]
    initializer.assert_not_called()


@pytest.mark.parametrize("case", ["oauth", "mismatched-model", "incomplete-other-provider"])
def test_login_rejects_incompatible_existing_configuration_before_writing(case: str) -> None:
    _ = document("settings.json", {"defaultProvider": "other"} if case == "incomplete-other-provider" else {})
    if case == "oauth":
        _ = document("auth.json", {"deepseek": {"type": "oauth", "access": "fake-token"}})
    elif case == "mismatched-model":
        _ = document("model-store.json", {"deepseek": {"models": [{
            "id": "first-model", "provider": "other", "baseUrl": "https://example.test",
        }]}})
    before = {path: path.read_bytes() for path in (settings.root / "agent").iterdir()}
    with pytest.raises(ValueError):
        _ = save_model_login(login_request())
    assert {path: path.read_bytes() for path in (settings.root / "agent").iterdir()} == before


def test_successful_login_invalidates_previously_cached_default_selection() -> None:
    _ = save_model_login(login_request())
    assert settings.load_settings().defaultModel == "first-model"
    _ = document("settings.json", {"defaultProvider": "deepseek", "defaultModel": "second-model"})
    _ = save_model_login(login_request(model_name="second-model"))
    assert settings.load_settings().defaultModel == "second-model"
    assert check_model_configuration().ready


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
