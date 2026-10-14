"""测试对象：src/omt/coding_agent/tools/write.py::write（LangChain 工具接口）。

覆盖内容：
1. 创建 UTF-8 文件、完整覆盖旧文件、空内容清空文件；接受 Path/字符串/相对路径。
2. 不自动创建父目录；缺失父目录或父路径为文件时报错。
3. 拒绝覆盖目录和非普通文件。
4. 内容类型非法或内容参数缺失时拒绝写入，旧内容保持不变。

隔离方式：全部通过 write.invoke() 调用，只使用 tmp_path 内的文件。
不写入真实项目、配置或凭据文件。
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from omt.coding_agent.tools.write import write


@pytest.mark.parametrize("use_string_path", [False, True])
def test_write_creates_utf8_file(tmp_path: Path, use_string_path: bool):
    file = tmp_path / "new.txt"

    write.invoke(
        {"file": str(file) if use_string_path else file, "content": "你好，Agent！\n第二行"}
    )

    assert file.read_text(encoding="utf-8") == "你好，Agent！\n第二行"


def test_write_resolves_relative_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)

    write.invoke({"file": "new.txt", "content": "相对路径"})

    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "相对路径"


def test_write_overwrites_instead_of_appending(tmp_path: Path):
    file = tmp_path / "example.txt"
    file.write_text("较长的原始内容，不应残留", encoding="utf-8")

    write.invoke({"file": file, "content": "新内容"})

    assert file.read_text(encoding="utf-8") == "新内容"


def test_empty_content_clears_existing_file(tmp_path: Path):
    file = tmp_path / "example.txt"
    file.write_text("原始内容", encoding="utf-8")

    write.invoke({"file": file, "content": ""})

    assert file.read_text(encoding="utf-8") == ""


def test_empty_content_can_create_empty_file(tmp_path: Path):
    file = tmp_path / "empty.txt"

    write.invoke({"file": file, "content": ""})

    assert file.is_file()
    assert file.read_text(encoding="utf-8") == ""


def test_missing_parent_directory_is_not_created(tmp_path: Path):
    parent = tmp_path / "missing"
    file = parent / "example.txt"

    with pytest.raises(FileNotFoundError, match="父目录不存在"):
        write.invoke({"file": file, "content": "新内容"})

    assert not parent.exists()
    assert not file.exists()


def test_parent_path_must_be_directory(tmp_path: Path):
    parent = tmp_path / "not-a-directory"
    parent.write_text("保留父文件", encoding="utf-8")

    with pytest.raises(NotADirectoryError, match="父路径不是目录"):
        write.invoke({"file": parent / "example.txt", "content": "新内容"})

    assert parent.read_text(encoding="utf-8") == "保留父文件"


def test_directory_cannot_be_overwritten(tmp_path: Path):
    directory = tmp_path / "directory"
    directory.mkdir()
    existing = directory / "keep.txt"
    existing.write_text("保留内容", encoding="utf-8")

    with pytest.raises(IsADirectoryError, match="路径指向目录"):
        write.invoke({"file": directory, "content": "新内容"})

    assert directory.is_dir()
    assert existing.read_text(encoding="utf-8") == "保留内容"


def test_non_regular_file_is_rejected(non_regular_file: Path):
    with pytest.raises(ValueError, match="路径不是普通文件"):
        write.invoke({"file": non_regular_file, "content": "新内容"})


@pytest.mark.parametrize("content", [None, 123, ["not", "a", "string"]])
def test_invalid_content_does_not_modify_existing_file(tmp_path: Path, content: object):
    file = tmp_path / "example.txt"
    file.write_text("原始内容", encoding="utf-8")

    with pytest.raises(ValidationError):
        write.invoke({"file": file, "content": content})

    assert file.read_text(encoding="utf-8") == "原始内容"


def test_missing_content_does_not_modify_existing_file(tmp_path: Path):
    file = tmp_path / "example.txt"
    file.write_text("原始内容", encoding="utf-8")

    with pytest.raises(ValidationError):
        write.invoke({"file": file})

    assert file.read_text(encoding="utf-8") == "原始内容"
