"""测试对象：src/omt/core/settings.py::Settings、load_settings。

覆盖内容：
1. Settings 接受有效的 provider/model，并拒绝缺字段、空 provider 和非法字段类型。
2. load_settings 从当前目录的 agent/settings.json 加载配置。
3. 文件缺失、JSON 非法、配置校验失败向上传播。
4. 成功加载被缓存；清空缓存后读取新内容；加载失败后可修复文件并重试。

隔离方式：只读写 tmp_path 下的配置；每例前后清空缓存并隔离相关环境变量。
不读取或修改真实 agent/settings.json、auth.json 或 .env。
"""

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from omt.core.settings import Settings, load_settings


@pytest.fixture
def settings_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    load_settings.cache_clear()
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.lower() in {"defaultprovider", "defaultmodel", "defaultthinkinglevel"}:
            monkeypatch.delenv(name)
    directory = tmp_path / "agent"
    directory.mkdir()
    try:
        yield directory / "settings.json"
    finally:
        load_settings.cache_clear()


def test_settings_accept_valid_values(settings_file: Path):
    settings = Settings(defaultProvider="deepseek", defaultModel="deepseek-chat")

    assert settings.defaultProvider == "deepseek"
    assert settings.defaultModel == "deepseek-chat"
    assert settings.defaultThinkingLevel is None


@pytest.mark.parametrize("level", [None, "media", "high", "none", "custom"])
def test_load_settings_reads_thinking_level(settings_file: Path, level: str | None) -> None:
    _ = settings_file.write_text(json.dumps({
        "defaultProvider": "deepseek", "defaultModel": "deepseek-chat",
        "defaultThinkingLevel": level,
    }), encoding="utf-8")

    assert load_settings().defaultThinkingLevel == level


@pytest.mark.parametrize("level", ["", "   ", 123])
def test_settings_reject_invalid_thinking_level(settings_file: Path, level: object) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({
            "defaultProvider": "deepseek", "defaultModel": "deepseek-chat",
            "defaultThinkingLevel": level,
        })


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"defaultModel": "deepseek-chat"}, "defaultProvider"),
        ({"defaultProvider": "deepseek"}, "defaultModel"),
        ({"defaultProvider": "", "defaultModel": "deepseek-chat"}, "defaultProvider"),
        ({"defaultProvider": None, "defaultModel": "deepseek-chat"}, "defaultProvider"),
        ({"defaultProvider": "deepseek", "defaultModel": None}, "defaultModel"),
        ({"defaultProvider": "deepseek", "defaultModel": 123}, "defaultModel"),
    ],
)
def test_settings_reject_invalid_values(
    payload: dict[str, object], field: str, settings_file: Path
) -> None:
    with pytest.raises(ValidationError) as error:
        _ = Settings.model_validate(payload)

    assert error.value.errors()[0]["loc"] == (field,)


def test_load_settings_reads_configuration(settings_file: Path):
    settings_file.write_text(
        json.dumps({"defaultProvider": "deepseek", "defaultModel": "deepseek-chat"}),
        encoding="utf-8",
    )

    settings = load_settings()

    assert isinstance(settings, Settings)
    assert settings.defaultProvider == "deepseek"
    assert settings.defaultModel == "deepseek-chat"
    assert settings.defaultThinkingLevel is None


def test_missing_settings_file_raises(settings_file: Path):
    with pytest.raises(FileNotFoundError):
        load_settings()


@pytest.mark.parametrize(
    "content",
    [
        "{broken json",
        '{"defaultProvider": "deepseek"}',
        '{"defaultProvider": "", "defaultModel": "x"}',
    ],
)
def test_invalid_settings_file_raises(content: str, settings_file: Path):
    settings_file.write_text(content, encoding="utf-8")

    with pytest.raises(ValidationError):
        load_settings()


def test_successful_load_is_cached_until_cache_is_cleared(settings_file: Path):
    settings_file.write_text(
        '{"defaultProvider": "deepseek", "defaultModel": "deepseek-chat"}',
        encoding="utf-8",
    )
    first = load_settings()
    settings_file.write_text(
        '{"defaultProvider": "deepseek", "defaultModel": "deepseek-reasoner"}',
        encoding="utf-8",
    )

    cached = load_settings()
    assert cached is first
    assert cached.defaultModel == "deepseek-chat"

    load_settings.cache_clear()
    refreshed = load_settings()
    assert refreshed is not first
    assert refreshed.defaultModel == "deepseek-reasoner"


def test_failed_load_can_be_retried_after_file_is_fixed(settings_file: Path):
    settings_file.write_text("{broken json", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings()

    settings_file.write_text(
        '{"defaultProvider": "deepseek", "defaultModel": "deepseek-chat"}',
        encoding="utf-8",
    )

    assert load_settings().defaultModel == "deepseek-chat"
