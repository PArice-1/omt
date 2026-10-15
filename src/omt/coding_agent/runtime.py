"""同步 Agent 会话：执行一轮对话、显示过程，并保留完整消息历史。"""

import json
from typing import Any
from uuid import uuid4

from langchain.agents.middleware.types import InputAgentState
from langchain_core.language_models.chat_model_stream import ChatModelStream
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from omt.agent.agent import Agent


def _format_output(value: object) -> str:
    """只格式化展示内容，不把消息元数据打印到终端，也不修改原始结果。"""
    if isinstance(value, BaseMessage):
        return value.text or _format_output(value.content)
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


class CreateAgentSession:
    """保存一个会话的完整内存历史，供同一实例的后续调用继续使用。

    每轮提交已有历史和新用户消息，成功后以最终状态中的 messages 替换历史。
    保留原始消息对象，包括 AIMessage.tool_calls、ToolMessage 和最终 AIMessage；
    不从终端文本或流式片段重建历史，也不自行执行工具。
    未提供 thread_id 时自动生成 UUID v4，同一 Session 的多轮调用复用该 ID。
    无需 checkpointer 即可在当前进程内连续对话；本类不负责写入磁盘。
    """

    def __init__(
        self,
        agent: Agent,
        config: RunnableConfig | None = None,
    ) -> None:
        self.agent: Agent = agent
        self.messages: list[AnyMessage] = []
        # 复制需要修改的两层字典，避免向调用者的原始配置写入 thread_id。
        self.config: RunnableConfig = {**(config or {})}
        configurable = dict(self.config.get("configurable", {}))
        if "thread_id" not in configurable:
            configurable["thread_id"] = str(uuid4())
        self.config["configurable"] = configurable

    def _display_agent_run(
        self,
        messages: list[AnyMessage],
    ) -> dict[str, Any] | None:
        """显示各次模型调用及工具执行，最终返回图状态而非拼接后的文本。"""
        input_state: InputAgentState = {"messages": [*messages]}
        with self.agent.stream_event(
            input=input_state,
            config=self.config,
        ) as stream:
            # 同时订阅两个通道，避免先消费完一个而错过另一个。
            for channel, item in stream.interleave("messages", "tool_calls"):
                if channel == "messages":
                    model_stream: ChatModelStream = item
                    has_text = False
                    for text in model_stream.text:
                        if not text:
                            continue
                        if not has_text:
                            print("[助手] ", end="", flush=True)
                            has_text = True
                        print(text, end="", flush=True)
                    if has_text:
                        print(flush=True)

                elif channel == "tool_calls":
                    print(f"[工具开始] {item.tool_name}", flush=True)
                    print(f"  参数：{_format_output(item.input)}", flush=True)

                    # 即使工具没有增量输出，也必须消费此通道，等待工具完成。
                    for delta in item.output_deltas:
                        print(
                            f"[工具输出] {item.tool_name}：{_format_output(delta)}",
                            flush=True,
                        )

                    output = item.output
                    if item.error is not None:
                        print(
                            f"[工具失败] {item.tool_name}：{item.error}",
                            flush=True,
                        )
                    elif isinstance(output, ToolMessage) and output.status == "error":
                        print(
                            f"[工具失败] {item.tool_name}：{_format_output(output)}",
                            flush=True,
                        )
                    else:
                        print(
                            f"[工具结果] {item.tool_name}\n{_format_output(output)}",
                            flush=True,
                        )
                        print(f"[工具完成] {item.tool_name}", flush=True)

            return stream.output

    def run(self, message: str) -> str:
        """同步执行一轮：实时显示过程，保存完整历史，只返回最后的模型回答文本。

        失败时保留上一轮已完成的历史并继续抛出原始异常；不会撤销工具副作用。
        """
        if not message.strip():
            raise ValueError("message不能为空")

        # 先准备输入，不提前把尚未完成的用户回合写入历史。
        input_messages = [*self.messages, HumanMessage(content=message)]
        try:
            result = self._display_agent_run(input_messages)
            if result is None:
                raise RuntimeError("Agent 未返回最终状态")

            messages: list[AnyMessage] = list(result["messages"])
            if not messages:
                raise RuntimeError("Agent 未返回最终 AIMessage")
            final_message = messages[-1]
            if not isinstance(final_message, AIMessage) or final_message.tool_calls:
                raise RuntimeError("Agent 未返回最终 AIMessage，可能仍处于工具调用或中断状态")

            # 完整历史照常保存；返回值只取最后一条模型回答，不包含工具日志。
            # 替换而不是追加，避免每轮重复保存已有消息。
            self.messages = messages
        except Exception as exc:
            print(f"\n[执行失败] {exc}", flush=True)
            raise

        return final_message.text
