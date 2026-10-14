"""测试对象：src/omt/coding_agent/tools/read.py::read（LangChain 工具接口）。

覆盖内容：
1. 读取 UTF-8 全文；接受 Path、字符串、相对路径和 ~ 路径。
2. offset 从 1 开始；空文件、超出末尾返回空串；无末尾换行的内容保留原样。
3. limit=True 最多返回 50 行，limit=False 不截断。
4. 缺失文件、目录、非普通文件，以及非法参数均拒绝读取。

隔离方式：全部通过 read.invoke() 调用，只使用 tmp_path 中的文件。
注意：参数先经过 LangChain/Pydantic 校验；不绕过 schema 测试内部函数。
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from omt.coding_agent.tools.read import read


@pytest.mark.parametrize("use_string_path", [False, True])
def test_read_returns_utf8_content(tmp_path: Path, use_string_path: bool):
    file = tmp_path / "example.txt"
    file.write_text("你好，Agent！\n第二行\n", encoding="utf-8")

    result = read.invoke({"file": str(file) if use_string_path else file})

    assert result == "你好，Agent！\n第二行\n"


def test_read_resolves_relative_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "example.txt").write_text("相对路径", encoding="utf-8")

    assert read.invoke({"file": "example.txt"}) == "相对路径"


def test_read_expands_home_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / "example.txt").write_text("测试 HOME", encoding="utf-8")

    assert read.invoke({"file": "~/example.txt"}) == "测试 HOME"


def test_offset_is_one_based_and_preserves_final_line(tmp_path: Path):
    file = tmp_path / "example.txt"
    file.write_text("第一行\n第二行\n最后一行", encoding="utf-8")

    assert read.invoke({"file": file, "offset": 2}) == "第二行\n最后一行"
    assert read.invoke({"file": file, "offset": 3}) == "最后一行"


def test_empty_file_returns_empty_string(tmp_path: Path):
    file = tmp_path / "empty.txt"
    file.write_text("", encoding="utf-8")

    assert read.invoke({"file": file}) == ""


def test_offset_past_end_returns_empty_string(tmp_path: Path):
    file = tmp_path / "example.txt"
    file.write_text("第一行\n第二行", encoding="utf-8")

    assert read.invoke({"file": file, "offset": 3}) == ""


def test_limit_returns_at_most_50_lines(tmp_path: Path):
    file = tmp_path / "long.txt"
    file.write_text("内容\n" * 60, encoding="utf-8")

    assert read.invoke({"file": file, "limit": True}) == "内容\n" * 50


def test_limit_starts_counting_from_offset(tmp_path: Path):
    file = tmp_path / "long.txt"
    file.write_text("跳过\n" * 5 + "读取\n" * 55, encoding="utf-8")

    assert read.invoke({"file": file, "offset": 6, "limit": True}) == "读取\n" * 50


def test_limit_does_not_pad_short_file(tmp_path: Path):
    file = tmp_path / "short.txt"
    file.write_text("第一行\n第二行", encoding="utf-8")

    assert read.invoke({"file": file, "limit": True}) == "第一行\n第二行"


def test_limit_false_reads_more_than_50_lines(tmp_path: Path):
    file = tmp_path / "long.txt"
    file.write_text("内容\n" * 60, encoding="utf-8")

    assert read.invoke({"file": file, "limit": False}) == "内容\n" * 60


def test_missing_file_is_rejected(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="路径不存在"):
        read.invoke({"file": tmp_path / "missing.txt"})


def test_directory_is_rejected(tmp_path: Path):
    with pytest.raises(IsADirectoryError, match="路径指向目录"):
        read.invoke({"file": tmp_path})


def test_non_regular_file_is_rejected(non_regular_file: Path):
    with pytest.raises(ValueError, match="路径不是普通文件"):
        read.invoke({"file": non_regular_file})


@pytest.mark.parametrize("offset", [0, -1])
def test_non_positive_offset_is_rejected(tmp_path: Path, offset: int):
    file = tmp_path / "example.txt"
    file.write_text("原始内容", encoding="utf-8")

    with pytest.raises(ValueError, match="offset 必须大于等于1"):
        read.invoke({"file": file, "offset": offset})


@pytest.mark.parametrize(
    "parameters",
    [
        {"offset": "not-an-integer"},
        {"offset": 1.5},
        {"limit": "not-a-boolean"},
    ],
)
def test_invalid_parameter_types_are_rejected(tmp_path: Path, parameters: dict):
    file = tmp_path / "example.txt"
    file.write_text("原始内容", encoding="utf-8")

    with pytest.raises(ValidationError):
        read.invoke({"file": file, **parameters})


def test_missing_file_argument_is_rejected():
    with pytest.raises(ValidationError):
        read.invoke({})
