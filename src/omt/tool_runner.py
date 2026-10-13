import inspect
from collections.abc import Callable
from typing import Any


class UnknownToolError(Exception):
    pass


class ArgsError(Exception):
    pass


class ToolExecutionError(Exception):
    pass


class ToolRunner:
    def __init__(self, tools: dict[str, Callable]):
        # 使用 self.add，注册已经绑定到实例的方法
       self.tools = tools

    def run(self, tool_name: str, args: dict[str, Any]) -> Any:
        # 1. 查找工具
        if tool_name not in self.tools:
            raise UnknownToolError(f"未知工具：{tool_name}")

        tool = self.tools[tool_name]

        # 2. 检查参数能否匹配函数签名
        signature = inspect.signature(tool)

        try:
            signature.bind(**args)
        except TypeError as exc:
            raise ArgsError(
                f"工具 {tool_name} 参数错误：{exc}"
            ) from exc

        # 3. 真正执行工具
        try:
            return tool(**args)
        except Exception as exc:
            raise ToolExecutionError(
                f"工具 {tool_name} 执行失败："
                f"{type(exc).__name__}: {exc}"
            ) from exc
