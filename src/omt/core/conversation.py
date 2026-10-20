"""入口无关的会话执行：消费模型/工具事件、校验终态并保留完整历史。"""

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain.agents.middleware.types import InputAgentState
from langchain_core.language_models.chat_model_stream import ChatModelStream
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig

from omt.agent.agent import Agent

from . import settings
from .events import EventHandler, RunEvent
from .skills import discover_skills, render_skills_prompt


class Conversation:
    """保存一个会话的完整内存历史，供同一实例的后续调用继续使用。

    每轮提交已有历史和新用户消息，成功后以最终状态中的 messages 替换历史。
    保留原始消息对象，包括 AIMessage.tool_calls、ToolMessage 和最终 AIMessage；
    不从终端文本或流式片段重建历史，也不自行执行工具。
    未提供 thread_id 时自动生成 UUID v4，同一 Session 的多轮调用复用该 ID。
    无需 checkpointer 即可在当前进程内连续对话；本类不负责写入磁盘。
    初始化时将可选提示词文本与 Skills 目录合并为首条 SystemMessage，空内容不插入。
    默认扫描用户和项目 Skills；传入 skill_roots=[] 可关闭扫描。
    同一份基础提示词不要同时传给 Agent.system_prompt 和本会话，避免重复发送。
    load_history() 可从检查点替换为另一段已完成会话；所有操作仅支持顺序调用。
    默认不展示过程，event_handler 可接收事件；终端渲染属于 cli.terminal。
    """

    def __init__(
        self,
        agent: Agent,
        config: RunnableConfig | None = None,
        *,
        system_prompt: str = "",
        skill_roots: Sequence[Path] | None = None,
        event_handler: EventHandler | None = None,
    ) -> None:
        """agent 是已组装的执行器，config 的 thread_id 决定检查点归属。

        system_prompt 是新会话的提示词正文，不是文件路径；skill_roots=None
        扫描默认目录，[] 关闭发现。event_handler 是同步快速回调，None 静默；
        它只接收过程，不得从片段重建历史，最终历史以图终态为准。
        """
        self._event_handler: EventHandler | None = event_handler
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
            self._emit(RunEvent("skills.diagnostic", text=diagnostic))

        skills_prompt = render_skills_prompt(result.skills)
        parts = [part for part in (system_prompt, skills_prompt) if part.strip()]
        if not parts:
            return []
        return [SystemMessage(content="\n\n".join(parts))]

    def _emit(self, event: RunEvent) -> None:
        if self._event_handler is not None:
            self._event_handler(event)

    def _execute(
        self,
        messages: list[AnyMessage],
    ) -> dict[str, Any] | None:
        """消费各次模型调用及工具执行，最终返回图状态而非拼接后的文本。"""
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
                            self._emit(RunEvent("assistant.started"))
                            has_text = True
                        self._emit(RunEvent("assistant.delta", text=text))
                    if has_text:
                        self._emit(RunEvent("assistant.completed"))

                elif channel == "tool_calls":
                    self._emit(RunEvent("tool.started", tool_name=item.tool_name, data=item.input))

                    # 即使没有接收函数，也必须消费此通道，等待工具完成。
                    for delta in item.output_deltas:
                        self._emit(RunEvent("tool.delta", tool_name=item.tool_name, data=delta))

                    self._emit_tool_result(item.tool_name, item.output, item.error)

            return stream.output

    async def _aexecute(
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

            async def consume_messages() -> None:
                async for model_stream in model_calls:
                    has_text = False
                    async for text in model_stream.text:
                        if not text:
                            continue
                        if not has_text:
                            self._emit(RunEvent("assistant.started"))
                            has_text = True
                        self._emit(RunEvent("assistant.delta", text=text))
                    if has_text:
                        self._emit(RunEvent("assistant.completed"))

            async def consume_tools() -> None:
                async for item in tool_calls:
                    self._emit(RunEvent("tool.started", tool_name=item.tool_name, data=item.input))
                    async for delta in item.output_deltas:
                        self._emit(RunEvent("tool.delta", tool_name=item.tool_name, data=delta))
                    self._emit_tool_result(item.tool_name, item.output, item.error)

            tasks = [
                asyncio.create_task(consume_messages()),
                asyncio.create_task(consume_tools()),
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
                        _ = task.cancel()
                _ = await asyncio.gather(*tasks, return_exceptions=True)

    def _emit_tool_result(
        self, tool_name: str, output: object, error: str | None
    ) -> None:
        """同步和异步路径使用相同的工具成功/失败事件。"""
        if error is not None:
            self._emit(RunEvent("tool.failed", tool_name=tool_name, text=error))
        elif isinstance(output, ToolMessage) and output.status == "error":
            self._emit(RunEvent("tool.failed", tool_name=tool_name, text=output.text, data=output))
        else:
            self._emit(RunEvent("tool.completed", tool_name=tool_name, data=output))

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
            result = await self._aexecute(input_messages)
            return self._commit_result(result)
        except Exception as exc:
            self._emit(RunEvent("run.failed", text=str(exc)))
            raise

    def run(self, message: str) -> str:
        """同步执行一轮：发送过程事件，保存完整历史，只返回最后的模型回答文本。

        失败时保留上一轮已完成的历史并继续抛出原始异常；不会撤销工具副作用。
        """
        if not message.strip():
            raise ValueError("message不能为空")
        # 先准备输入，不提前把尚未完成的用户回合写入历史。
        input_messages = [*self.messages, HumanMessage(content=message)]
        try:
            result = self._execute(input_messages)
            return self._commit_result(result)
        except Exception as exc:
            self._emit(RunEvent("run.failed", text=str(exc)))
            raise

    async def load_history(self, thread_id: str) -> None:
        """按 thread_id 加载最新已完成检查点；只加载数据，不打印历史。

        不执行模型或工具，不重新扫描 Skills，不读写会话索引。
        目标缺失、执行未完成、查询失败或被取消时保留当前会话；异常向外传播。
        不支持与 run、arun 或另一次切换并发调用，调用方应先等待当前操作结束。
        """
        if not thread_id.strip():
            raise ValueError("thread_id 不能为空")

        config: RunnableConfig = {**self.config}
        configurable = dict(config.get("configurable", {}))
        # 切换会话时不能沿用旧快照、子图命名空间或检查点映射。
        for key in ("checkpoint_id", "checkpoint_ns", "checkpoint_map"):
            _ = configurable.pop(key, None)
        configurable["thread_id"] = thread_id
        config["configurable"] = configurable
        snapshot = await self.agent.agent.aget_state(config)
        if snapshot.next:
            raise RuntimeError("目标会话存在未完成的执行，请先处理恢复")
        messages: list[AnyMessage] = list(snapshot.values.get("messages", []))
        if not messages:
            raise ValueError("目标会话不存在或没有可恢复的 messages")
        final_message = messages[-1]
        if not isinstance(final_message, AIMessage) or final_message.tool_calls:
            raise RuntimeError("目标会话历史未完成，缺少最终 AIMessage")

        # 查询和校验都成功后才更新；保留原始消息及其工具调用、附加数据和 ID。
        self.messages, self.config = messages, config

    def get_config(self) -> RunnableConfig:
        """返回当前运行配置；应用入口由 Manager 注入 ID，不反向读取来建索引。"""
        return self.config
