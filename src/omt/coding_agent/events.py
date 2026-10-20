"""执行事件与默认终端渲染；事件不是持久化消息，也不替代最终状态。"""

import json
from dataclasses import dataclass
from typing import Literal

from langchain_core.messages import BaseMessage


@dataclass(frozen=True)
class RunEvent:
    kind: Literal[
        "assistant.started", "assistant.delta", "assistant.completed",
        "tool.started", "tool.delta", "tool.completed", "tool.failed", "run.failed",
    ]
    text: str = ""
    tool_name: str = ""
    data: object = None


def format_output(value: object) -> str:
    """只格式化展示内容，不打印消息对象元数据，不修改原始结果。"""
    if isinstance(value, BaseMessage):
        return value.text or format_output(value.content)
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def print_event(event: RunEvent) -> None:
    """保留 CLI 的既有展示；HTTP 可不安装此接收函数。"""
    match event.kind:
        case "assistant.started":
            print("[助手] ", end="", flush=True)
        case "assistant.delta":
            print(event.text, end="", flush=True)
        case "assistant.completed":
            print(flush=True)
        case "tool.started":
            print(f"[工具开始] {event.tool_name}", flush=True)
            print(f"  参数：{format_output(event.data)}", flush=True)
        case "tool.delta":
            print(f"[工具输出] {event.tool_name}：{format_output(event.data)}", flush=True)
        case "tool.failed":
            print(f"[工具失败] {event.tool_name}：{event.text}", flush=True)
        case "tool.completed":
            print(f"[工具结果] {event.tool_name}\n{format_output(event.data)}", flush=True)
            print(f"[工具完成] {event.tool_name}", flush=True)
        case "run.failed":
            print(f"\n[执行失败] {event.text}", flush=True)
