"""通过 AST 发现工具定义，并导入对应的 LangChain 工具。"""


from langchain_core.tools import BaseTool

from .tools.edit import edit
from .tools.read import read
from .tools.write import write

def load_tools() -> list[BaseTool]:
    """返回内置工具，每次调用创建一个新的列表"""
    return [edit, read, write]



''' 动态扫描
import ast
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Annotated

from langchain_core.tools import BaseTool
from pydantic import Field

from .utils.find_files import find_files





@dataclass(frozen=True)
class ToolDefinition:
    file: Annotated[Path, Field(description="函数所在文件路径")]
    python_name: Annotated[str, Field(min_length=1, description="Python 中的函数名")]
    description: Annotated[str, Field(min_length=1, description="函数文档字符串")]
    line: Annotated[int, Field(ge=1, description="def 所在行号")]


def _is_tool_decorator(node: ast.expr) -> bool:
    """识别 @tool、@tool(...) 及模块限定的 @module.tool。"""
    if isinstance(node, ast.Call):
        node = node.func

    return (isinstance(node, ast.Name) and node.id == "tool") or (
        isinstance(node, ast.Attribute) and node.attr == "tool"
    )


def _parse_tool_file(file: Path) -> list[ToolDefinition]:
    """只解析模块顶层的工具函数，不执行文件中的代码。"""
    source = file.read_text(encoding="utf-8")
    tree = ast.parse(source=source, filename=str(file))

    result: list[ToolDefinition] = []

    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        if not any(_is_tool_decorator(item) for item in node.decorator_list):
            continue

        result.append(
            ToolDefinition(
                file=file,
                python_name=node.name,
                description=ast.get_docstring(node) or "",
                line=node.lineno,
            )
        )

    return result


def _scan_tools(root: str | Path) -> list[ToolDefinition]:
    """递归扫描工具目录，按文件路径和定义顺序返回结果。"""
    result: list[ToolDefinition] = []

    for file in find_files(root, "**/*.py"):
        result.extend(_parse_tool_file(file))

    return result


def _load_tools(
    definitions: list[ToolDefinition],
    source_root: str | Path,
) -> dict[str, BaseTool]:
    """从可导入的源码根目录加载工具，并拒绝重复的工具名称。

    source_root 应为 Python 导入路径中的目录，例如项目的 src 目录。
    此函数不修改 sys.path，也不忽略解析或导入错误。
    """
    source_root = Path(source_root).expanduser().resolve()
    registry: dict[str, BaseTool] = {}

    for definition in definitions:
        relative = definition.file.expanduser().resolve().relative_to(source_root)

        # 包的 __init__.py 对应本身
        if relative.name == "__init__.py":
            parts = relative.parent.parts
        else:
            parts = relative.with_suffix("").parts

        module_name = ".".join(parts)
        if not module_name:
            raise ValueError(f"无法确定模块名：{definition.file}")

        module = import_module(module_name)
        obj = getattr(module, definition.python_name)

        if not isinstance(obj, BaseTool):
            raise TypeError(
                f"{module_name}.{definition.python_name} "
                "不是 LangChain 工具"
            )

        if obj.name in registry:
            raise ValueError(f"工具重复：{obj.name} {definition.file}")

        registry[obj.name] = obj

    return registry


# def get_tools() -> list[BaseTool]:
#     """返回内置工具列表；路径不依赖调用方的当前工作目录。"""
#     module_file = Path(__file__).resolve()
#     definitions = _scan_tools(module_file.parent / "tools")
#     registry = _load_tools(definitions, source_root=module_file.parents[2])
#     return list(registry.values())
'''


