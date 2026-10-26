"""与入口无关的过程事件；事件不是持久化消息或完整图状态。"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class RunEvent:
    kind: Literal[
        "assistant.started", "assistant.delta", "assistant.reasoning.delta", "assistant.completed",
        "tool.started", "tool.delta", "tool.completed", "tool.failed", "run.failed",
        "skills.diagnostic",
    ]
    text: str = ""
    tool_name: str = ""
    data: object = None
    message_id: str | None = None
    tool_call_id: str | None = None


# 接收函数必须快速返回；当前不提供异步背压或网络事件传输。
EventHandler = Callable[[RunEvent], None]
