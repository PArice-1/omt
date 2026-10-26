"""入口无关的会话执行：消费模型/工具事件、校验终态并保留完整历史。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from langchain.agents.middleware.types import InputAgentState
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig

from omt.agent.agent import Agent

from . import settings
from .events import EventHandler, RunEvent
from .skills import discover_skills, render_skills_prompt

if TYPE_CHECKING:
    from langchain_protocol.protocol import MessagesData, ToolsData
    from langgraph.stream import ProtocolEvent


class _MessageEvents:
    """按模型内容块顺序投影正文和 reasoning；finish 只补未发送的尾部。"""

    def __init__(self, emit: EventHandler, message_id: str | None) -> None:
        self.emit: EventHandler = emit
        self.message_id: str = message_id or str(uuid4())
        self.started: bool = False
        self.parts: dict[tuple[str, int], str] = {}

    def consume(self, event: MessagesData) -> None:
        if event["event"] == "message-start":
            if not self.started:
                self.message_id = event["id"]
        elif event["event"] == "content-block-delta":
            delta = event["delta"]
            if delta["type"] == "text-delta":
                self.emit_delta("text", event["index"], delta["text"])
            elif delta["type"] == "reasoning-delta":
                self.emit_delta("reasoning", event["index"], delta["reasoning"])
        elif event["event"] == "content-block-finish":
            block = event["content"]
            if block["type"] == "text":
                self._finish("text", event["index"], block["text"])
            elif block["type"] == "reasoning":
                self._finish("reasoning", event["index"], block.get("reasoning", ""))

    def _finish(self, kind: Literal["text", "reasoning"], index: int, value: str) -> None:
        previous = self.parts.get((kind, index), "")
        if value.startswith(previous):
            self.emit_delta(kind, index, value[len(previous):])

    def emit_delta(self, kind: Literal["text", "reasoning"], index: int, value: str) -> None:
        if not value:
            return
        if not self.started:
            self.emit(RunEvent("assistant.started", message_id=self.message_id))
            self.started = True
        key = (kind, index)
        self.parts[key] = self.parts.get(key, "") + value
        event_kind = "assistant.delta" if kind == "text" else "assistant.reasoning.delta"
        self.emit(RunEvent(event_kind, text=value, message_id=self.message_id))

    def complete(self) -> None:
        if self.started:
            self.emit(RunEvent("assistant.completed", message_id=self.message_id))


class _RunEvents:
    """同步/异步共用的根图原始事件投影，不订阅惰性的每工具子通道。"""

    def __init__(self, emit: EventHandler) -> None:
        self.emit: EventHandler = emit
        self.models: dict[str, _MessageEvents] = {}
        self.tools: dict[str, str] = {}

    def consume(self, event: ProtocolEvent) -> None:
        # 与原根级 messages/tool_calls 投影保持一致，不混入子图。
        if event["params"]["namespace"]:
            return
        if event["method"] == "messages":
            # 根图消息通道是 (协议片段或完整消息, metadata)，不是直接的 MessagesData。
            entry: tuple[MessagesData | BaseMessage, dict[str, object]] = event["params"]["data"]
            payload, metadata = entry
            if isinstance(payload, AIMessage) and not isinstance(payload, AIMessageChunk):
                self._whole_message(payload)
            elif isinstance(payload, dict):
                run_id = metadata.get("run_id", "")
                key = run_id if isinstance(run_id, str) else ""
                if payload["event"] == "message-start" and payload["role"] == "ai":
                    self.models[key] = _MessageEvents(self.emit, payload["id"])
                model = self.models.get(key)
                if model is not None:
                    model.consume(payload)
                    if payload["event"] == "message-finish":
                        model.complete()
                        del self.models[key]
        elif event["method"] == "tools":
            data: ToolsData = event["params"]["data"]
            identifier = data["tool_call_id"]
            if data["event"] == "tool-started":
                self.tools[identifier] = data["tool_name"]
                self.emit(RunEvent("tool.started", tool_name=data["tool_name"], data=data.get("input"),
                                   tool_call_id=identifier))
            elif data["event"] == "tool-output-delta":
                self.emit(RunEvent("tool.delta", tool_name=self.tools.get(identifier, identifier),
                                   data=data["delta"], tool_call_id=identifier))
            elif data["event"] == "tool-error":
                self.emit(RunEvent("tool.failed", tool_name=self.tools.pop(identifier, identifier),
                                   text=data["message"], tool_call_id=identifier))
            elif data["event"] == "tool-finished":
                name = self.tools.pop(identifier, identifier)
                output: object = data["output"]
                if isinstance(output, ToolMessage) and output.status == "error":
                    self.emit(RunEvent("tool.failed", tool_name=name, text=output.text, data=output,
                                       tool_call_id=identifier))
                else:
                    self.emit(RunEvent("tool.completed", tool_name=name, data=output,
                                       tool_call_id=identifier))

    def _whole_message(self, message: AIMessage) -> None:
        """非原生分片模型可能只返回完整消息；按标准块补发一次，不依赖子通道回放。"""
        projection = _MessageEvents(self.emit, message.id)
        blocks = message.content_blocks
        if not any(block["type"] == "reasoning" for block in blocks):
            legacy = message.additional_kwargs.get("reasoning_content")
            if isinstance(legacy, str):
                projection.emit_delta("reasoning", -1, legacy)
        for index, block in enumerate(blocks):
            if block["type"] == "text":
                projection.emit_delta("text", index, block["text"])
            elif block["type"] == "reasoning":
                projection.emit_delta("reasoning", index, block.get("reasoning", ""))
        projection.complete()


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

    def _execute(self, messages: list[AnyMessage]) -> dict[str, Any] | None:
        """单次订阅根图原始事件，避免并行工具在子通道订阅前丢失进度。"""
        input_state: InputAgentState = {"messages": [*messages]}
        projection = _RunEvents(self._emit)
        with self.agent.stream_event(input=input_state, config=self.config) as stream:
            for event in stream:
                projection.consume(event)
            return stream.output

    async def _aexecute(self, messages: list[AnyMessage]) -> dict[str, Any] | None:
        """单一消费者保持事件顺序；取消时由图流上下文回收底层执行。"""
        input_state: InputAgentState = {"messages": [*messages]}
        projection = _RunEvents(self._emit)
        async with await self.agent.astream_event(input=input_state, config=self.config) as stream:
            async for event in stream:
                projection.consume(event)
            return await stream.output()

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
