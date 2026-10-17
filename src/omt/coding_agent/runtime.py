"""同步/异步 Agent 会话：执行一轮对话、显示过程，并保留完整消息历史。"""

import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain.agents.middleware.types import InputAgentState
from langchain_core.language_models.chat_model_stream import ChatModelStream
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig

from omt.agent.agent import Agent

from . import settings
from .skills import discover_skills, render_skills_prompt


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
    初始化时将可选提示词文本与 Skills 目录合并为首条 SystemMessage，空内容不插入。
    默认扫描用户和项目 Skills；传入 skill_roots=[] 可关闭扫描。
    同一份基础提示词不要同时传给 Agent.system_prompt 和本会话，避免重复发送。
    """

    def __init__(
        self,
        agent: Agent,
        config: RunnableConfig | None = None,
        *,
        system_prompt: str = "",
        skill_roots: Sequence[Path] | None = None,
    ) -> None:
        self.agent: Agent = agent
        self.messages: list[AnyMessage] = self._prepare_messages(system_prompt, skill_roots)
        # 复制需要修改的两层字典，避免向调用者的原始配置写入 thread_id。
        self.config: RunnableConfig = {**(config or {})}
        configurable = dict(self.config.get("configurable", {}))
        if "thread_id" not in configurable:
            configurable["thread_id"] = str(uuid4())
        self.config["configurable"] = configurable

    def _prepare_messages(
        self,
        system_prompt: str,
        skill_roots: Sequence[Path] | None,
    ) -> list[AnyMessage]:
        """初始化一次提示词与 Skills 目录，不加载正文、不恢复磁盘历史。"""
        if skill_roots is None:
            # 复用配置根目录，保持路径一致且允许测试隔离；项目版本最后覆盖。
            skill_roots = [
                settings.root / "agent" / "skills",
                Path.cwd() / "skills",
            ]
        result = discover_skills(skill_roots)
        for diagnostic in result.diagnostics:
            print(f"[Skills] {diagnostic}", file=sys.stderr)

        skills_prompt = render_skills_prompt(result.skills)
        parts = [part for part in (system_prompt, skills_prompt) if part.strip()]
        if not parts:
            return []
        return [SystemMessage(content="\n\n".join(parts))]

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

                    self._display_tool_result(item.tool_name, item.output, item.error)

            return stream.output

    async def _adisplay_agent_run(
        self,
        messages: list[AnyMessage],
    ) -> dict[str, Any] | None:
        """并行消费异步模型与工具通道，退出前回收所有消费任务和图流。"""
        input_state: InputAgentState = {"messages": [*messages]}
        async with await self.agent.astream_event(
            input=input_state,
            config=self.config,
        ) as stream:
            # 异步 v3 暂无 interleave；推进图之前先订阅两个通道，防止丢事件。
            model_calls = aiter(stream.messages)
            tool_calls = aiter(stream.tool_calls)

            async def display_messages() -> None:
                async for model_stream in model_calls:
                    has_text = False
                    async for text in model_stream.text:
                        if not text:
                            continue
                        if not has_text:
                            print("[助手] ", end="", flush=True)
                            has_text = True
                        print(text, end="", flush=True)
                    if has_text:
                        print(flush=True)

            async def display_tools() -> None:
                async for item in tool_calls:
                    print(f"[工具开始] {item.tool_name}", flush=True)
                    print(f"  参数：{_format_output(item.input)}", flush=True)
                    async for delta in item.output_deltas:
                        print(
                            f"[工具输出] {item.tool_name}：{_format_output(delta)}",
                            flush=True,
                        )
                    self._display_tool_result(item.tool_name, item.output, item.error)

            tasks = [
                asyncio.create_task(display_messages()),
                asyncio.create_task(display_tools()),
            ]
            try:
                # 两个通道都读完再抛原异常，避免另一个通道的工具失败通知被取消。
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        raise result
                return await stream.output()
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    def _display_tool_result(
        self, tool_name: str, output: object, error: str | None
    ) -> None:
        """同步和异步路径使用相同的工具成功/失败展示规则。"""
        if error is not None:
            print(f"[工具失败] {tool_name}：{error}", flush=True)
        elif isinstance(output, ToolMessage) and output.status == "error":
            print(f"[工具失败] {tool_name}：{_format_output(output)}", flush=True)
        else:
            print(f"[工具结果] {tool_name}\n{_format_output(output)}", flush=True)
            print(f"[工具完成] {tool_name}", flush=True)

    def _commit_result(self, result: dict[str, Any] | None) -> str:
        """只在拿到无待执行工具的最终 AIMessage 后替换历史，返回最后回答。"""
        if result is None:
            raise RuntimeError("Agent 未返回最终状态")
        messages: list[AnyMessage] = list(result["messages"])
        if not messages:
            raise RuntimeError("Agent 未返回最终 AIMessage")
        final_message = messages[-1]
        if not isinstance(final_message, AIMessage) or final_message.tool_calls:
            raise RuntimeError("Agent 未返回最终 AIMessage，可能仍处于工具调用或中断状态")
        self.messages = messages
        return final_message.text

    async def arun(self, message: str) -> str:
        """异步执行一轮，与 run 共享历史契约；失败或取消不提交本轮历史。

        同一实例仅支持顺序调用；CancelledError 原样传播，不被当作普通失败。
        关闭流不保证同步工具线程或已经发生的外部副作用被撤销。
        """
        if not message.strip():
            raise ValueError("message不能为空")
        input_messages = [*self.messages, HumanMessage(content=message)]
        try:
            result = await self._adisplay_agent_run(input_messages)
            return self._commit_result(result)
        except Exception as exc:
            print(f"\n[执行失败] {exc}", flush=True)
            raise

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
            return self._commit_result(result)
        except Exception as exc:
            print(f"\n[执行失败] {exc}", flush=True)
            raise
