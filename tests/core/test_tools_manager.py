"""测试对象：src/omt/core/tools_manager.py::load_tools。

覆盖内容：
1. 返回名为 edit/read/write/bash 的 LangChain BaseTool 列表，无重复名称。
2. 每次返回独立列表，修改一个列表不会影响后续加载。
3. 在其他工作目录下仍能加载工具，并通过工具调用完成写入、读取、编辑的闭环。

隔离方式：通过 load_tools 和返回工具的 invoke 接口验证；文件只放在 tmp_path。
不测试或恢复已经停用的 AST 动态扫描实现。
"""

from pathlib import Path

import pytest
from langchain_core.tools import BaseTool

from omt.core.tools_manager import load_tools


def test_load_tools_returns_builtin_langchain_tools():
    tools = load_tools()

    assert isinstance(tools, list)
    assert len(tools) == 4
    assert {tool.name for tool in tools} == {"edit", "read", "write", "bash"}
    assert all(isinstance(tool, BaseTool) for tool in tools)


def test_load_tools_returns_independent_lists():
    first = load_tools()
    second = load_tools()

    assert first is not second
    first.clear()
    assert len(second) == 4
    assert {tool.name for tool in load_tools()} == {"edit", "read", "write", "bash"}


def test_loaded_tools_work_outside_project_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    tools = {tool.name: tool for tool in load_tools()}
    file = tmp_path / "example.txt"

    tools["write"].invoke({"file": "example.txt", "content": "工具加载成功"})
    assert tools["read"].invoke({"file": "example.txt"}) == "工具加载成功"

    tools["edit"].invoke(
        {
            "file": "example.txt",
            "old_string": "加载",
            "new_string": "执行",
            "replace_all": False,
        }
    )
    assert tools["read"].invoke({"file": "example.txt"}) == "工具执行成功"
    assert file.read_text(encoding="utf-8") == "工具执行成功"
