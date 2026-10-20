"""Agent 封装：显式提供常用的创建和调用参数，其余参数透传给底层。"""

from collections.abc import Callable, Sequence
from typing import Any, Literal

from langchain.agents import create_agent
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    InputAgentState,
    OutputAgentState,
)
from langchain.agents.structured_output import ResponseFormat
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from langgraph.stream import AsyncGraphRunStream, GraphRunStream
from langgraph.types import All, Checkpointer, Command, Durability, StreamMode


_AgentGraph = CompiledStateGraph[
    AgentState[Any], Any, InputAgentState, OutputAgentState[Any]
]


class Agent:
    """支持同步/异步调用和 v3 事件流的 Agent 封装。

    model 支持模型实例或提供商模型字符串，tools 支持工具对象、函数及工具字典。
    checkpointer 保存单个会话的状态，store 用于跨会话存储。
    context_schema 描述运行上下文，state_schema 用于扩展 Agent 状态。
    不对 LangChain/LangGraph 对象额外做 Pydantic 转换或校验。
    """

    model: str | BaseChatModel
    tools: Sequence[BaseTool | Callable[..., Any] | dict[str, Any]] | None
    agent: _AgentGraph

    def __init__(
        self,
        model: str | BaseChatModel,
        tools: Sequence[BaseTool | Callable[..., Any] | dict[str, Any]] | None = None,
        *,
        system_prompt: str | SystemMessage | None = None,
        middleware: Sequence[AgentMiddleware[Any, Any, Any]] = (),
        response_format: ResponseFormat[Any] | type[Any] | dict[str, Any] | None = None,
        state_schema: type[AgentState[Any]] | None = None,
        context_schema: type[Any] | None = None,
        checkpointer: Checkpointer = None,
        store: BaseStore | None = None,
        interrupt_before: list[str] | None = None,
        interrupt_after: list[str] | None = None,
        debug: bool = False,
        name: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.model = model
        self.tools = tools
        self.agent = self._init_agent(
            model,
            tools,
            system_prompt=system_prompt,
            middleware=middleware,
            response_format=response_format,
            state_schema=state_schema,
            context_schema=context_schema,
            checkpointer=checkpointer,
            store=store,
            interrupt_before=interrupt_before,
            interrupt_after=interrupt_after,
            debug=debug,
            name=name,
            **kwargs,
        )

    def _init_agent(
        self,
        model: str | BaseChatModel,
        tools: Sequence[BaseTool | Callable[..., Any] | dict[str, Any]] | None,
        **kwargs: Any,
    ) -> _AgentGraph:
        return create_agent(model=model, tools=tools, **kwargs)

    def invoke(
        self,
        input: InputAgentState | Command[Any] | None,
        config: RunnableConfig | None = None,
        *,
        context: Any = None,
        stream_mode: StreamMode = "values",
        print_mode: StreamMode | Sequence[StreamMode] = (),
        output_keys: str | Sequence[str] | None = None,
        interrupt_before: All | Sequence[str] | None = None,
        interrupt_after: All | Sequence[str] | None = None,
        durability: Durability | None = None,
        **kwargs: Any,
    ) -> Any:
        """同步执行 Agent 图，保持底层 invoke 的参数语义和返回结果。

        input 通常为包含 messages 的状态字典；Command 和 None 用于恢复执行。
        config 可设置会话 thread_id、回调和递归限制；context 与 context_schema 对应。
        stream_mode 默认为 values，返回最终状态；其他模式返回收集到的事件。
        print_mode 仅用于打印调试信息，output_keys 用于限制返回的状态字段。
        interrupt_before/after 可指定节点名称或 "*"；None 沿用图的配置。
        durability 控制检查点写入时机，可为 sync、async、exit 或底层默认值 None。
        不常用的运行参数（例如 version）可通过 kwargs 透传。
        """
        return self.agent.invoke(
            input=input,
            config=config,
            context=context,
            stream_mode=stream_mode,
            print_mode=print_mode,
            output_keys=output_keys,
            interrupt_before=interrupt_before,
            interrupt_after=interrupt_after,
            durability=durability,
            **kwargs,
        )

    async def ainvoke(
        self,
        input: InputAgentState | Command[Any] | None,
        config: RunnableConfig | None = None,
        *,
        context: Any = None,
        stream_mode: StreamMode = "values",
        print_mode: StreamMode | Sequence[StreamMode] = (),
        output_keys: str | Sequence[str] | None = None,
        interrupt_before: All | Sequence[str] | None = None,
        interrupt_after: All | Sequence[str] | None = None,
        durability: Durability | None = None,
        **kwargs: Any,
    ) -> Any:
        """异步执行 Agent 图，参数和返回值沿用底层 ainvoke 语义。

        可同时执行同步工具和异步工具；异常及任务取消直接向调用方传播。
        """
        return await self.agent.ainvoke(
            input=input,
            config=config,
            context=context,
            stream_mode=stream_mode,
            print_mode=print_mode,
            output_keys=output_keys,
            interrupt_before=interrupt_before,
            interrupt_after=interrupt_after,
            durability=durability,
            **kwargs,
        )

    async def astream_event(
        self,
        input: InputAgentState | Command[Any] | None,
        config: RunnableConfig | None = None,
        *,
        context: Any = None,
        version: Literal["v3"] = "v3",
        print_mode: StreamMode | Sequence[StreamMode] = (),
        output_keys: str | Sequence[str] | None = None,
        interrupt_before: All | Sequence[str] | None = None,
        interrupt_after: All | Sequence[str] | None = None,
        durability: Durability | None = None,
        **kwargs: Any,
    ) -> AsyncGraphRunStream:
        """获取原始异步 v3 流，不在包装层消费事件或创建后台任务。

        使用 async with await agent.astream_event(...) as run 管理生命周期，
        async for 消费投影，await run.output() 获取终态；提前退出会关闭流。
        """
        if version != "v3":
            raise ValueError("astream_event 仅支持异步 v3 事件流")
        return await self.agent.astream_events(
            input=input,
            config=config,
            context=context,
            version=version,
            print_mode=print_mode,
            output_keys=output_keys,
            interrupt_before=interrupt_before,
            interrupt_after=interrupt_after,
            durability=durability,
            **kwargs,
        )

    def stream_event(
        self,
        input: InputAgentState | Command[Any] | None,
        config: RunnableConfig | None = None,
        *,
        context: Any = None,
        version: Literal["v3"] = "v3",
        print_mode: StreamMode | Sequence[StreamMode] = (),
        output_keys: str | Sequence[str] | None = None,
        interrupt_before: All | Sequence[str] | None = None,
        interrupt_after: All | Sequence[str] | None = None,
        durability: Durability | None = None,
        **kwargs: Any,
    ) -> GraphRunStream:
        """返回同步 v3 事件流，由调用者迭代驱动执行，不使用异步或后台线程。

        推荐 with agent.stream_event(...) as run，再用 for event in run 消费原始事件，
        也可读取 run.messages、run.values 等通道；run.output 会推进执行并返回最终状态。
        提前停止时，with 会关闭流；未使用 with 时应调用 run.abort()。
        context、输出字段、中断和持久化参数与 invoke 对应。
        v3 是实验性接口，事件格式为 method/params，不是 v2 的 event/data。
        不支持旧版事件过滤参数，也不能传入 stream_mode 或 subgraphs，通道由 v3 管理。
        """
        if version != "v3":
            raise ValueError("stream_event 仅支持同步 v3 事件流")
        return self.agent.stream_events(
            input=input,
            config=config,
            context=context,
            version=version,
            print_mode=print_mode,
            output_keys=output_keys,
            interrupt_before=interrupt_before,
            interrupt_after=interrupt_after,
            durability=durability,
            **kwargs,
        )
