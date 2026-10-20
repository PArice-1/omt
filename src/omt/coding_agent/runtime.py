"""同步/异步 Agent 会话：执行一轮对话、显示过程，并保留完整消息历史。"""

import asyncio
import sys
from collections.abc import Callable, Sequence
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
from .skills import discover_skills, render_skills_prompt
from .events import RunEvent, format_output as _format_output, print_event


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
    switch_session() 可从检查点替换为另一段已完成会话；所有操作仅支持顺序调用。
    event_handler 默认使用终端渲染；传入 None 静默执行，传入函数接收结构化过程事件。
    """

    def __init__(
        self,
        agent: Agent,
        config: RunnableConfig | None = None,
        *,
        system_prompt: str = "",
        skill_roots: Sequence[Path] | None = None,
        event_handler: Callable[[RunEvent], None] | None = print_event,
    ) -> None:
        self._event_handler: Callable[[RunEvent], None] | None = event_handler
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

    def _emit(self, event: RunEvent) -> None:
        if self._event_handler is not None:
            self._event_handler(event)

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
                            self._emit(RunEvent("assistant.started"))
                            has_text = True
                        self._emit(RunEvent("assistant.delta", text=text))
                    if has_text:
                        self._emit(RunEvent("assistant.completed"))

            async def display_tools() -> None:
                async for item in tool_calls:
                    self._emit(RunEvent("tool.started", tool_name=item.tool_name, data=item.input))
                    async for delta in item.output_deltas:
                        self._emit(RunEvent("tool.delta", tool_name=item.tool_name, data=delta))
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
            self._emit(RunEvent("tool.failed", tool_name=tool_name, text=error))
        elif isinstance(output, ToolMessage) and output.status == "error":
            self._emit(RunEvent("tool.failed", tool_name=tool_name, text=_format_output(output)))
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
            result = await self._adisplay_agent_run(input_messages)
            return self._commit_result(result)
        except Exception as exc:
            self._emit(RunEvent("run.failed", text=str(exc)))
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
            self._emit(RunEvent("run.failed", text=str(exc)))
            raise

    def display_history(self) -> None:
        """按消息顺序展示当前历史，不输出系统提示词、artifact 或模型元数据。"""
        messages = [message for message in self.messages if not isinstance(message, SystemMessage)]
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
                    print(f"  参数：{_format_output(call['args'])}", flush=True)
            elif isinstance(message, ToolMessage):
                label = "工具失败" if message.status == "error" else "工具结果"
                name = message.name or message.tool_call_id
                print(f"[{label}] {name}\n{_format_output(message)}", flush=True)
            else:
                label = "用户" if isinstance(message, HumanMessage) else message.type
                print(f"[{label}] {_format_output(message)}", flush=True)

    async def switch_session(self, thread_id: str, *, display: bool = True) -> None:
        """加载目标会话的最新根图检查点，校验后替换历史和配置。

        display 默认 True 保留 CLI 展示；HTTP 传 False，不输出历史正文。

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
        if display:
            self.display_history()

    def get_config(self) -> RunnableConfig:
        """返回当前运行配置；CLI 应在组装时注入 ID，不依赖反向读取来建索引。"""
        return self.config
