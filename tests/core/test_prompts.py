"""内置提示词资源测试；不读取用户提示词或调用模型。"""

from pathlib import Path

import pytest

from omt.core import prompts as prompts_module
from omt.core.prompts import load_system_prompt


@pytest.fixture
def resource_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """以临时目录代替包资源入口，覆盖缺失、编码和内容错误。"""
    def files(anchor: str) -> Path:
        assert anchor == "omt.core.prompts"
        return tmp_path

    monkeypatch.setattr(prompts_module, "files", files)
    return tmp_path


def test_load_system_prompt_uses_bundled_resource_outside_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # cwd 下的同名文件不能取代内置的可信资源。
    _ = (tmp_path / "system.md").write_text("LOCAL_OVERRIDE", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    prompt = load_system_prompt()

    assert prompt.startswith("# OMT Coding Agent\n")
    assert "read" in prompt
    assert "edit" in prompt
    assert "write" in prompt
    assert "bash" in prompt
    assert "LOCAL_OVERRIDE" not in prompt


def test_load_system_prompt_preserves_unicode_and_formatting(
    resource_directory: Path,
) -> None:
    content = "  测试用系统提示词。\n\n保留段落和首尾空白。\n"
    _ = (resource_directory / "system.md").write_text(content, encoding="utf-8")

    assert load_system_prompt() == content


@pytest.mark.parametrize("content", ["", " ", "\n\t"])
def test_load_system_prompt_rejects_empty_resource(
    resource_directory: Path, content: str
) -> None:
    _ = (resource_directory / "system.md").write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="内置系统提示词不能为空"):
        _ = load_system_prompt()


def test_load_system_prompt_propagates_missing_resource(resource_directory: Path) -> None:
    assert not (resource_directory / "system.md").exists()

    with pytest.raises(FileNotFoundError):
        _ = load_system_prompt()


def test_load_system_prompt_rejects_invalid_utf8(resource_directory: Path) -> None:
    _ = (resource_directory / "system.md").write_bytes(b"\xff\xfe")

    with pytest.raises(UnicodeDecodeError):
        _ = load_system_prompt()


def test_load_system_prompt_rereads_resource_without_cache(
    resource_directory: Path,
) -> None:
    file = resource_directory / "system.md"
    _ = file.write_text("旧提示词", encoding="utf-8")
    assert load_system_prompt() == "旧提示词"
    _ = file.write_text("新提示词", encoding="utf-8")

    assert load_system_prompt() == "新提示词"
