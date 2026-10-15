"""配置读取与校验，只使用临时目录中的假凭据和模型目录。"""

import json
import traceback
from pathlib import Path

import pytest
from pydantic import ValidationError

from omt.coding_agent.settings import Settings, load_auth, load_model_store


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / "agent"
    directory.mkdir()
    return directory


def test_auth_loads_api_key_as_masked_secret(config_dir: Path):
    key = "test-only-not-a-real-secret"
    (config_dir / "auth.json").write_text(json.dumps({
        "deepseek": {"type": "api_key", "key": key},
    }), encoding="utf-8")

    auth = load_auth()["deepseek"]

    assert auth.type == "api_key"
    assert auth.key is not None
    assert auth.key.get_secret_value() == key
    assert key not in repr(auth)


def test_auth_retains_type_without_treating_oauth_as_api_key(config_dir: Path):
    (config_dir / "auth.json").write_text(json.dumps({
        "deepseek": {"type": "oauth", "access": "test-access", "refresh": "test-refresh"},
    }), encoding="utf-8")

    auth = load_auth()["deepseek"]

    assert auth.type == "oauth"
    assert auth.key is None
    assert "test-access" not in repr(auth)
    assert "test-refresh" not in repr(auth)


@pytest.mark.parametrize("payload", [
    '{"deepseek": {"type": "api_key", "key": "test-sensitive-marker",}}',
    '{"deepseek": {"key": "test-sensitive-marker"}}',
    '{"deepseek": "test-sensitive-marker"}',
])
def test_invalid_auth_errors_do_not_expose_credentials(config_dir: Path, payload: str):
    (config_dir / "auth.json").write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match="auth.json") as raised:
        load_auth()

    formatted_error = "".join(traceback.format_exception(raised.value))
    assert "test-sensitive-marker" not in formatted_error


def test_missing_auth_file_raises(config_dir: Path):
    with pytest.raises(FileNotFoundError):
        load_auth()


def test_auth_is_reread_after_credentials_change(config_dir: Path):
    path = config_dir / "auth.json"
    path.write_text('{"deepseek":{"type":"api_key","key":"first-test-key"}}')
    first = load_auth()["deepseek"]
    path.write_text('{"deepseek":{"type":"api_key","key":"second-test-key"}}')
    second = load_auth()["deepseek"]

    assert first.key is not None and second.key is not None
    assert first.key.get_secret_value() == "first-test-key"
    assert second.key.get_secret_value() == "second-test-key"


def test_model_store_parses_model_url_and_ignores_catalog_metadata(config_dir: Path):
    (config_dir / "model-store.json").write_text(json.dumps({
        "deepseek": {
            "models": [{
                "id": "deepseek-chat", "provider": "deepseek",
                "baseUrl": "https://example.test/v1", "reasoning": True,
                "cost": {"input": 1}, "compat": {"supportsStore": False},
            }],
            "checkedAt": 123,
        }
    }), encoding="utf-8")

    model = load_model_store()["deepseek"].models[0]

    assert model.id == "deepseek-chat"
    assert model.provider == "deepseek"
    assert str(model.baseUrl) == "https://example.test/v1"


@pytest.mark.parametrize("content", [
    "{broken-json",
    "[]",
    '{"deepseek": {"models": [{"id":"x"}]}}',
    '{"deepseek": {"models": [{"id":"x","baseUrl":"ftp://example.test"}]}}',
    '{"deepseek": {"models": [{"id":"","baseUrl":"https://example.test"}]}}',
    '{"deepseek": {"models": ['
    '{"id":"same","baseUrl":"https://example.test"},'
    '{"id":"same","baseUrl":"https://example.test"}]}}',
])
def test_invalid_model_store_is_rejected(config_dir: Path, content: str):
    (config_dir / "model-store.json").write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="model-store.json"):
        load_model_store()


def test_missing_model_store_file_raises(config_dir: Path):
    with pytest.raises(FileNotFoundError):
        load_model_store()


@pytest.mark.parametrize("field", ["defaultProvider", "defaultModel"])
@pytest.mark.parametrize("value", ["", "   "])
def test_settings_reject_blank_selection(field: str, value: str):
    payload = {"defaultProvider": "deepseek", "defaultModel": "deepseek-chat", field: value}

    with pytest.raises(ValidationError):
        Settings(**payload)
