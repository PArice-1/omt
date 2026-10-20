"""测试对象：src/omt/core/tools/edit.py::edit（LangChain 工具接口）。

覆盖内容：
1. 单处精确替换、授权的多处替换、空 new_string 删除及相对路径。
2. 空 old_string、没有匹配、未授权多处匹配均报错，原文件保持不变。
3. 缺失文件、目录、非普通文件均拒绝编辑。
4. replace_all 缺失或参数类型非法时拒绝修改，保留原内容。

隔离方式：全部通过 edit.invoke() 调用；只修改 tmp_path 下的测试文件。
不测试内部 _check_file，不假设当前尚未实现的工作目录访问限制。
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from omt.core.tools.edit import edit


@pytest.mark.parametrize("use_string_path", [False, True])
def test_edit_replaces_unique_match(tmp_path: Path, use_string_path: bool):
    file = tmp_path / "example.txt"
    file.write_text("第一行\n旧内容\n最后一行", encoding="utf-8")

    edit.invoke(
        {
            "file": str(file) if use_string_path else file,
            "old_string": "旧内容",
            "new_string": "新内容",
            "replace_all": False,
        }
    )

    assert file.read_text(encoding="utf-8") == "第一行\n新内容\n最后一行"


def test_edit_resolves_relative_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    file = tmp_path / "example.txt"
    file.write_text("旧内容", encoding="utf-8")

    edit.invoke(
        {"file": "example.txt", "old_string": "旧内容", "new_string": "新内容", "replace_all": False}
    )

    assert file.read_text(encoding="utf-8") == "新内容"


def test_edit_replaces_all_matches_when_authorized(tmp_path: Path):
    file = tmp_path / "example.txt"
    file.write_text("旧内容\n保留\n旧内容", encoding="utf-8")

    edit.invoke(
        {"file": file, "old_string": "旧内容", "new_string": "新内容", "replace_all": True}
    )

    assert file.read_text(encoding="utf-8") == "新内容\n保留\n新内容"


def test_empty_replacement_deletes_match(tmp_path: Path):
    file = tmp_path / "example.txt"
    file.write_text("前缀[删除]后缀", encoding="utf-8")

    edit.invoke(
        {"file": file, "old_string": "[删除]", "new_string": "", "replace_all": False}
    )

    assert file.read_text(encoding="utf-8") == "前缀后缀"


@pytest.mark.parametrize(
    ("content", "old_string", "replace_all", "message"),
    [
        ("原始内容", "", False, "替换文本不能为空"),
        ("原始内容", "不存在的文本", False, "未找到匹配文本"),
        ("原始内容", "不存在的文本", True, "未找到匹配文本"),
        ("重复\n重复", "重复", False, "匹配到 2 处"),
    ],
)
def test_rejected_edit_preserves_original_content(
    tmp_path: Path, content: str, old_string: str, replace_all: bool, message: str
):
    file = tmp_path / "example.txt"
    file.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        edit.invoke(
            {
                "file": file,
                "old_string": old_string,
                "new_string": "新内容",
                "replace_all": replace_all,
            }
        )

    assert file.read_text(encoding="utf-8") == content


def test_missing_file_is_rejected_without_creating_it(tmp_path: Path):
    file = tmp_path / "missing.txt"

    with pytest.raises(FileNotFoundError, match="路径不存在"):
        edit.invoke(
            {"file": file, "old_string": "旧内容", "new_string": "新内容", "replace_all": False}
        )

    assert not file.exists()


def test_directory_is_rejected(tmp_path: Path):
    with pytest.raises(IsADirectoryError, match="路径指向目录"):
        edit.invoke(
            {"file": tmp_path, "old_string": "旧内容", "new_string": "新内容", "replace_all": False}
        )


def test_non_regular_file_is_rejected(non_regular_file: Path):
    with pytest.raises(ValueError, match="路径不是普通文件"):
        edit.invoke(
            {
                "file": non_regular_file,
                "old_string": "旧内容",
                "new_string": "新内容",
                "replace_all": False,
            }
        )


@pytest.mark.parametrize(
    "invalid_parameters",
    [
        {"replace_all": "not-a-boolean"},
        {"old_string": None},
        {"new_string": None},
    ],
)
def test_invalid_parameters_preserve_file(tmp_path: Path, invalid_parameters: dict):
    file = tmp_path / "example.txt"
    file.write_text("原始内容", encoding="utf-8")
    parameters = {
        "file": file,
        "old_string": "原始内容",
        "new_string": "新内容",
        "replace_all": False,
        **invalid_parameters,
    }

    with pytest.raises(ValidationError):
        edit.invoke(parameters)

    assert file.read_text(encoding="utf-8") == "原始内容"


def test_missing_replace_all_is_rejected_without_modification(tmp_path: Path):
    file = tmp_path / "example.txt"
    file.write_text("原始内容", encoding="utf-8")

    with pytest.raises(ValidationError):
        edit.invoke({"file": file, "old_string": "原始内容", "new_string": "新内容"})

    assert file.read_text(encoding="utf-8") == "原始内容"
