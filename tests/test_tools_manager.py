import ast
from pathlib import Path
from uuid import uuid4

import pytest
from langchain_core.tools import BaseTool

from omt.coding_agent.tools_manager import (
    _is_tool_decorator,
    _load_tools,
    _parse_tool_file,
    _scan_tools,
    get_tools,
)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("tool", True),
        ('tool("custom_name")', True),
        ("tools.tool", True),
        ("tools.tool(parse_docstring=True)", True),
        ("other", False),
        ("tools.other()", False),
    ],
)
def test_is_tool_decorator(expression: str, expected: bool):
    node = ast.parse(expression, mode="eval").body
    assert _is_tool_decorator(node) is expected


def test_parse_tool_file_without_executing_code(tmp_path: Path):
    file = tmp_path / "tools.py"
    file.write_text(
        'raise RuntimeError("此文件不应执行")\n'
        "@tool\n"
        "def first():\n"
        '    """同步工具。"""\n'
        "    pass\n"
        '\n@tool("custom_name")\n'
        "async def second():\n"
        '    """异步工具。"""\n'
        "    pass\n"
        "\ndef helper():\n"
        "    @tool\n"
        "    def nested():\n"
        "        pass\n"
        "\nclass Example:\n"
        "    @tool\n"
        "    def method(self):\n"
        "        pass\n",
        encoding="utf-8",
    )

    definitions = _parse_tool_file(file)

    assert [item.python_name for item in definitions] == ["first", "second"]
    assert [item.description for item in definitions] == ["同步工具。", "异步工具。"]
    assert [item.line for item in definitions] == [3, 8]
    assert all(item.file == file for item in definitions)


def test_parse_tool_without_docstring(tmp_path: Path):
    file = tmp_path / "tools.py"
    file.write_text("@tool\ndef example():\n    pass\n", encoding="utf-8")

    assert _parse_tool_file(file)[0].description == ""


def test_scan_tools_recursively_and_ignore_cache(tmp_path: Path):
    for name in ["z.py", "nested/a.py", "__pycache__/cached.py", ".venv/tool.py", ".git/tool.py"]:
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(
            '@tool\ndef example():\n    """工具。"""\n    pass\n',
            encoding="utf-8",
        )

    definitions = _scan_tools(tmp_path)

    assert [item.file.relative_to(tmp_path).as_posix() for item in definitions] == [
        "nested/a.py", "z.py",
    ]


def test_scan_empty_directory(tmp_path: Path):
    assert _scan_tools(tmp_path) == []


def test_scan_missing_directory(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        _scan_tools(tmp_path / "missing")


def test_scan_propagates_syntax_error(tmp_path: Path):
    file = tmp_path / "broken.py"
    file.write_text("def broken(\n", encoding="utf-8")

    with pytest.raises(SyntaxError) as exc_info:
        _scan_tools(tmp_path)

    assert exc_info.value.filename == str(file)


@pytest.fixture
def tool_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    package = tmp_path / f"tool_fixture_{uuid4().hex}"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    return package


def test_load_tools_from_modules_and_package_init(tool_package: Path):
    (tool_package / "__init__.py").write_text(
        "from langchain_core.tools import tool\n"
        '@tool("package_tool")\n'
        'def package_function() -> str:\n    """包工具。"""\n    return "package"\n',
        encoding="utf-8",
    )
    nested = tool_package / "nested"
    nested.mkdir()
    (nested / "example.py").write_text(
        "from langchain_core import tools\n"
        '@tools.tool("custom_name")\n'
        'def python_name() -> str:\n    """同步工具。"""\n    return "sync"\n'
        "\n@tools.tool\n"
        'async def async_tool() -> str:\n    """异步工具。"""\n    return "async"\n',
        encoding="utf-8",
    )

    registry = _load_tools(_scan_tools(tool_package), tool_package.parent)

    assert list(registry) == ["package_tool", "custom_name", "async_tool"]
    assert all(isinstance(item, BaseTool) for item in registry.values())
    assert registry["package_tool"].invoke({}) == "package"
    assert registry["custom_name"].invoke({}) == "sync"
    assert registry["async_tool"].coroutine is not None


def test_load_tools_rejects_duplicate_names(tool_package: Path):
    (tool_package / "duplicate.py").write_text(
        "from langchain_core.tools import tool\n"
        '@tool("duplicate")\n'
        'def first() -> str:\n    """第一个工具。"""\n    return "first"\n'
        '@tool("duplicate")\n'
        'def second() -> str:\n    """第二个工具。"""\n    return "second"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="工具重复：duplicate"):
        _load_tools(_scan_tools(tool_package), tool_package.parent)


def test_load_tools_rejects_non_langchain_tool(tool_package: Path):
    (tool_package / "invalid.py").write_text(
        "def tool(function):\n    return function\n"
        "@tool\ndef example():\n    pass\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="不是 LangChain 工具"):
        _load_tools(_scan_tools(tool_package), tool_package.parent)


def test_load_tools_rejects_files_outside_source_root(tool_package: Path):
    file = tool_package / "example.py"
    file.write_text("@tool\ndef example():\n    pass\n", encoding="utf-8")

    with pytest.raises(ValueError):
        _load_tools(_parse_tool_file(file), tool_package / "other")


def test_load_tools_rejects_source_root_init(tmp_path: Path):
    file = tmp_path / "__init__.py"
    file.write_text("@tool\ndef example():\n    pass\n", encoding="utf-8")

    with pytest.raises(ValueError, match="无法确定模块名"):
        _load_tools(_parse_tool_file(file), tmp_path)


def test_get_tools_is_independent_of_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)

    tools = get_tools()

    assert isinstance(tools, list)
    assert [item.name for item in tools] == ["edit", "read", "write"]
    assert all(isinstance(item, BaseTool) for item in tools)
    file = tmp_path / "example.txt"
    file.write_text("工具加载成功", encoding="utf-8")
    read_tool = next(item for item in tools if item.name == "read")
    assert read_tool.invoke({"file": str(file)}) == "工具加载成功"
