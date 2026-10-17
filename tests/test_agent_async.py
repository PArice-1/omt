"""异步 Agent 接口：本地模型、内存检查点和工具，不调用外部服务。"""

import asyncio
from inspect import signature
from typing import Self, override
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.stream import AsyncGraphRunStream
from langgraph.types import Command

from omt.agent.agent import Agent


class ToolModel(FakeMessagesListChatModel):
    @override
    def bind_tools(self, tools: object, **kwargs: object) -> Self:
        return self


@pytest.mark.parametrize("input_value", [
    {"messages": [{"role": "user", "content": "你好"}]},
    Command(resume="继续"),
    None,
])
@pytest.mark.parametrize("method", ["ainvoke", "astream_event"])
def test_async_interfaces_forward_options_and_preserve_result(input_value, method: str) -> None:
    agent = Agent(FakeListChatModel(responses=["未使用"]))
    graph = Mock()
    backend = AsyncMock()
    setattr(graph, "ainvoke" if method == "ainvoke" else "astream_events", backend)
    agent.agent = graph
    config = {"configurable": {"thread_id": "async-forward"}}
    context = {"user_id": "local-user"}
    options = {
        "context": context,
        "print_mode": "values",
        "output_keys": ["messages"],
        "interrupt_before": "*",
        "interrupt_after": ["tools"],
        "durability": "sync",
        "debug": True,
    }
    if method == "ainvoke":
        options["stream_mode"] = "updates"
        options["version"] = "v2"
    else:
        options["version"] = "v3"

    result = asyncio.run(getattr(agent, method)(input_value, config, **options))

    assert result is backend.return_value
    backend.assert_awaited_once_with(input=input_value, config=config, **options)
    assert backend.call_args.kwargs["input"] is input_value
    assert backend.call_args.kwargs["config"] is config
    assert backend.call_args.kwargs["context"] is context
    graph.invoke.assert_not_called()
    graph.stream_events.assert_not_called()


@pytest.mark.parametrize("method", ["ainvoke", "astream_event"])
def test_async_interfaces_preserve_backend_error(method: str) -> None:
    agent = Agent(FakeListChatModel(responses=["未使用"]))
    error = RuntimeError("异步图调用失败")
    backend = AsyncMock(side_effect=error)
    graph = Mock()
    setattr(graph, "ainvoke" if method == "ainvoke" else "astream_events", backend)
    agent.agent = graph

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(getattr(agent, method)({"messages": []}))

    assert raised.value is error


def test_ainvoke_defaults_match_underlying_function() -> None:
    underlying = signature(CompiledStateGraph.ainvoke).parameters
    for name, parameter in signature(Agent.ainvoke).parameters.items():
        if name not in {"self", "kwargs"}:
            assert parameter.default == underlying[name].default
            assert parameter.kind == underlying[name].kind


@pytest.mark.parametrize("streaming", [False, True])
def test_async_graph_executes_async_only_and_sync_tools(streaming: bool) -> None:
    @tool
    async def async_note(text: str) -> str:
        """异步返回本地说明。"""
        await asyncio.sleep(0)
        return f"异步：{text}"

    @tool
    def sync_note(text: str) -> str:
        """同步返回本地说明。"""
        return f"同步：{text}"

    agent = Agent(ToolModel(responses=[
        AIMessage(content="", tool_calls=[
            {"name": "async_note", "args": {"text": "甲"}, "id": "async-call"},
            {"name": "sync_note", "args": {"text": "乙"}, "id": "sync-call"},
        ]),
        AIMessage(content="汇总完成"),
    ]), tools=[async_note, sync_note])

    async def execute() -> None:
        if streaming:
            async with await agent.astream_event({"messages": [HumanMessage(content="执行")]}) as run:
                result = await run.output()
        else:
            result = await agent.ainvoke({"messages": [HumanMessage(content="执行")]})
        assert result is not None
        assert result["messages"][-1].text == "汇总完成"
        results = {
            msg.tool_call_id: msg.text
            for msg in result["messages"] if isinstance(msg, ToolMessage)
        }
        assert results == {"async-call": "异步：甲", "sync-call": "同步：乙"}

    asyncio.run(execute())


def test_astream_event_is_lazy_and_streams_text_and_final_state() -> None:
    model = FakeListChatModel(responses=["异步回答", "未使用"])
    agent = Agent(model)

    async def execute() -> None:
        async with await agent.astream_event({"messages": [HumanMessage(content="你好")]}) as run:
            assert isinstance(run, AsyncGraphRunStream)
            assert model.i == 0
            chunks: list[str] = []
            async for message in run.messages:
                async for text in message.text:
                    chunks.append(text)
            result = await run.output()
            assert result is not None
            assert result["messages"][-1].text == "异步回答"
        assert chunks == list("异步回答")
        assert model.i == 1

    asyncio.run(execute())


def test_astream_event_raw_events_and_early_exit_close_stream() -> None:
    model = FakeListChatModel(responses=["不应执行", "未使用"])
    agent = Agent(model)

    async def execute() -> None:
        async with await agent.astream_event({"messages": [HumanMessage(content="你好")]}) as run:
            cursor = aiter(run)
            first = await anext(cursor)
            assert first["method"] == "values"
            assert "params" in first
        assert [event async for event in cursor] == []
        await run.abort()
        assert model.i == 0

    asyncio.run(execute())


def test_async_checkpointer_interrupt_resume_and_second_turn() -> None:
    agent = Agent(
        FakeListChatModel(responses=["恢复回答", "第二轮回答"]),
        checkpointer=InMemorySaver(),
    )
    config: RunnableConfig = {"configurable": {"thread_id": "async-interrupt"}}

    async def execute() -> None:
        async with await agent.astream_event(
            {"messages": [HumanMessage(content="第一轮")]}, config, interrupt_before=["model"]
        ) as paused:
            result = await paused.output()
            assert result is not None
            assert len(result["messages"]) == 1
        async with await agent.astream_event(None, config, interrupt_before=[]) as resumed:
            result = await resumed.output()
            assert result is not None
            assert result["messages"][-1].text == "恢复回答"
        second = await agent.ainvoke({"messages": [HumanMessage(content="第二轮")]}, config)
        assert [msg.text for msg in second["messages"]] == [
            "第一轮", "恢复回答", "第二轮", "第二轮回答"
        ]

    asyncio.run(execute())


@pytest.mark.parametrize("version", ["v1", "v2", "unknown"])
def test_astream_event_rejects_other_protocols(version) -> None:
    agent = Agent(FakeListChatModel(responses=["未使用"]))
    graph = Mock()
    agent.agent = graph

    with pytest.raises(ValueError, match="仅支持异步 v3"):
        asyncio.run(agent.astream_event({"messages": []}, version=version))

    graph.astream_events.assert_not_called()
