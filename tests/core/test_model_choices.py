"""共用快捷模型目录：只读、本地、过滤无凭据/未注册项。"""

import json

import pytest
from pydantic import SecretStr

from omt.core import model_configuration, settings


def test_configured_choices_are_read_only_and_include_the_full_catalog() -> None:
    assert model_configuration.list_configured_models() == []
    assert not (settings.root / "agent").exists()
    model_configuration.save_model_login(
        model_configuration.ModelLogin(provider="deepseek", api_key=SecretStr("test-only-key"), model_name="deepseek-flash"),
        model_names=["unknown", "deepseek-v4-pro", "deepseek-flash"],
    )
    directory = settings.root / "agent"
    before = {path: path.read_bytes() for path in directory.iterdir()}
    choices = model_configuration.list_configured_models()
    assert [(item.provider, item.model) for item in choices] == [
        ("deepseek", "deepseek-flash"), ("deepseek", "deepseek-v4-pro"), ("deepseek", "unknown"),
    ]
    assert choices[0].available_levels == ("none", "low", "high", "max")
    assert choices[-1].available_levels == ("none",)
    assert before == {path: path.read_bytes() for path in directory.iterdir()}


@pytest.mark.parametrize(("mapping", "expected"), [
    (None, ("none",)),
    ({}, ("none",)),
    ({"max": "max", "high": "high", "low": "low"}, ("none", "low", "high", "max")),
    ({"none": None, "high": None, "low": "max"}, ("none", "low")),
    ({"z-custom": "low", "medium": "high", "high": "low", "a-custom": "max",
      "low": "high", "max": "max", "unsupported": None},
     ("none", "low", "medium", "high", "max", "a-custom", "z-custom")),
    ({"z-custom": "low", "a-custom": "max"}, ("none", "a-custom", "z-custom")),
])
def test_thinking_levels_use_strength_order_and_preserve_custom_levels(
    mapping: dict[str, str | None] | None, expected: tuple[str, ...],
) -> None:
    directory = settings.root / "agent"
    directory.mkdir(parents=True)
    _ = (directory / "auth.json").write_text(json.dumps({
        "deepseek": {"type": "api_key", "key": "test-only-key"},
    }), encoding="utf-8")
    _ = (directory / "model-store.json").write_text(json.dumps({
        "deepseek": {"models": [{
            "id": "local", "baseUrl": "https://example.test", "thinkingLevelMap": mapping,
        }]},
    }), encoding="utf-8")
    before = {path: path.read_bytes() for path in directory.iterdir()}
    choices = model_configuration.list_configured_models()
    assert len(choices) == 1 and choices[0].available_levels == expected
    assert before == {path: path.read_bytes() for path in directory.iterdir()}


@pytest.mark.parametrize("key", [None, "", " "])
def test_choices_exclude_models_without_saved_credentials(key: str | None) -> None:
    directory = settings.root / "agent"
    directory.mkdir(parents=True)
    (directory / "model-store.json").write_text(json.dumps({
        "deepseek": {"models": [{"id": "local", "baseUrl": "https://example.test"}]},
    }), encoding="utf-8")
    if key is not None:
        (directory / "auth.json").write_text(json.dumps({"deepseek": {"type": "api_key", "key": key}}), encoding="utf-8")
    assert model_configuration.list_configured_models() == []
    assert not (directory / "settings.json").exists()


def test_choices_skip_unregistered_and_mismatched_provider_entries() -> None:
    directory = settings.root / "agent"
    directory.mkdir(parents=True)
    (directory / "auth.json").write_text(json.dumps({
        name: {"type": "api_key", "key": "test-only-key"} for name in ("deepseek", "other")
    }), encoding="utf-8")
    (directory / "model-store.json").write_text(json.dumps({
        "deepseek": {"models": [{"id": "valid", "baseUrl": "https://example.test"},
                                 {"id": "mismatch", "provider": "other", "baseUrl": "https://example.test"}]},
        "other": {"models": [{"id": "other", "baseUrl": "https://example.test"}]},
    }), encoding="utf-8")
    assert [item.model for item in model_configuration.list_configured_models()] == ["valid"]
