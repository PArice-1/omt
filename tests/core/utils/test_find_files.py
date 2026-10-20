"""测试对象：src/omt/core/utils/find_files.py::find_files。

覆盖内容：
1. 按 glob 模式查找普通文件，支持递归模式，返回排序后的绝对 Path 列表。
2. 任意层级的 .git/.venv/__pycache__ 中的文件被过滤，目录本身不作为文件返回。
3. 非递归模式、空目录和无匹配模式的行为。
4. 支持字符串、Path、相对路径和 ~ 路径。
5. 根目录缺失时报 FileNotFoundError；根路径为文件时，当前实现报 FileExistsError。

隔离方式：仅使用 tmp_path 中的目录树，不扫描真实项目。
这里记录已实现的接口行为，不假设此函数已注册为 Agent 工具。
"""

from pathlib import Path

import pytest

from omt.core.utils.find_files import find_files


@pytest.mark.parametrize("use_string_path", [False, True])
def test_recursive_search_returns_sorted_absolute_files(
    tmp_path: Path, use_string_path: bool
):
    for name in ["z.py", "nested/b.py", "a.py", "notes.txt"]:
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("测试文件", encoding="utf-8")
    (tmp_path / "directory.py").mkdir()

    result = find_files(str(tmp_path) if use_string_path else tmp_path, "**/*.py")

    assert result == [tmp_path / "a.py", tmp_path / "nested/b.py", tmp_path / "z.py"]
    assert all(isinstance(file, Path) and file.is_absolute() for file in result)


def test_ignored_directories_are_filtered_at_every_depth(tmp_path: Path):
    for name in [
        ".git/hidden.py",
        ".venv/hidden.py",
        "__pycache__/hidden.py",
        "nested/.git/hidden.py",
        "nested/.venv/hidden.py",
        "nested/__pycache__/hidden.py",
        "nested/visible.py",
        "visible.py",
    ]:
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("测试文件", encoding="utf-8")

    assert find_files(tmp_path, "**/*.py") == [
        tmp_path / "nested/visible.py",
        tmp_path / "visible.py",
    ]


def test_non_recursive_pattern_only_returns_direct_children(tmp_path: Path):
    (tmp_path / "visible.py").write_text("测试文件", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "hidden.py").write_text("测试文件", encoding="utf-8")

    assert find_files(tmp_path, "*.py") == [tmp_path / "visible.py"]


def test_empty_directory_returns_empty_list(tmp_path: Path):
    assert find_files(tmp_path, "**/*") == []


def test_pattern_without_matches_returns_empty_list(tmp_path: Path):
    (tmp_path / "example.txt").write_text("测试文件", encoding="utf-8")

    assert find_files(tmp_path, "**/*.py") == []


def test_relative_root_is_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "example.py").write_text("测试文件", encoding="utf-8")

    assert find_files(".", "*.py") == [tmp_path / "example.py"]


def test_home_root_is_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / "example.py").write_text("测试文件", encoding="utf-8")

    assert find_files("~", "*.py") == [tmp_path / "example.py"]


def test_missing_root_is_rejected(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="目录不存在"):
        find_files(tmp_path / "missing", "**/*")


def test_file_cannot_be_used_as_root(tmp_path: Path):
    file = tmp_path / "example.py"
    file.write_text("保留内容", encoding="utf-8")

    # 当前实现使用 FileExistsError；若将来统一为 NotADirectoryError，再同步此约定。
    with pytest.raises(FileExistsError, match="不是目录"):
        find_files(file, "**/*")

    assert file.read_text(encoding="utf-8") == "保留内容"
