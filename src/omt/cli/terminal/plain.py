"""终端输入校验、过程渲染和历史展示；不读数据库、不执行 Agent。"""

import json
import sys
from collections.abc import Sequence
from typing import override

from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from prompt_toolkit import PromptSession
from prompt_toolkit.document import Document
from prompt_toolkit.validation import ValidationError, Validator

from omt.core.bootstrap import ThinkingStatus
from omt.core.events import RunEvent
from omt.core.session_manager import validate_message


class InputValidator(Validator):
    @override
    def validate(self, document: Document) -> None:
        try:
            _ = validate_message(document.text)
        except ValueError as exc:
            raise ValidationError(message=str(exc), cursor_position=len(document.text)) from exc


def read_input_sync(terminal: PromptSession[str] | None = None) -> str:
    """读取一次有效输入；terminal 允许注入内存终端，默认创建真实交互终端。"""
    terminal = terminal if terminal is not None else PromptSession()
    return terminal.prompt("You> ", validator=InputValidator(), validate_while_typing=False)


async def read_input(terminal: PromptSession[str] | None = None) -> str:
    """异步读取输入，Ctrl-C/EOF 交给入口处理，合法原文不裁剪。"""
    terminal = terminal if terminal is not None else PromptSession()
    return await terminal.prompt_async("You> ", validator=InputValidator(), validate_while_typing=False)


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
        case "skills.diagnostic":
            print(f"[Skills] {event.text}", file=sys.stderr)
        case "run.failed":
            print(f"\n[执行失败] {event.text}", flush=True)


def display_history(history: Sequence[AnyMessage]) -> None:
    """按消息顺序展示当前历史，不输出系统提示词、artifact 或模型元数据。"""
    messages = [message for message in history if not isinstance(message, SystemMessage)]
    print("\n[对话历史]", flush=True)
    if not messages:
        print("暂无对话历史。", flush=True)
        return

    for message in messages:
        if isinstance(message, AIMessage):
            if message.text:
                print(f"[助手] {message.text}", flush=True)
            for call in message.tool_calls:
                print(f"[工具调用] {call['name']}", flush=True)
                print(f"  参数：{format_output(call['args'])}", flush=True)
        elif isinstance(message, ToolMessage):
            label = "工具失败" if message.status == "error" else "工具结果"
            name = message.name or message.tool_call_id
            print(f"[{label}] {name}\n{format_output(message)}", flush=True)
        else:
            label = "用户" if isinstance(message, HumanMessage) else message.type
            print(f"[{label}] {format_output(message)}", flush=True)


def display_thinking(status: ThinkingStatus) -> None:
    """显示请求等级和实际映射值；none 的请求语义由厂商适配负责。"""
    print(f"模型：{status.provider} / {status.model}")
    print(f"推理等级：{status.level}")
    print(f"实际 reasoning_effort：{status.reasoning_effort}")
    print(f"可选等级：{'、'.join(status.available_levels)}")
    if status.level != "none" and status.level not in status.available_levels:
        print("当前模型无有效映射，按 none 处理。")
    if status.reasoning_effort == "none":
        print("none 的请求行为由厂商适配决定，具体以模型协议为准。")


def print_warnings(warnings: Sequence[str]) -> None:
    """仅渲染已知业务警告，避免把已完成回合显示成执行失败。"""
    if "session_timestamp_update_failed" in warnings:
        print("[修改时间更新失败] 操作已完成，会话仍可继续。", flush=True)
