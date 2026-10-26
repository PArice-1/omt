"""通过公开 Conversation 调用核对 reasoning 增量、正文和完整历史。"""

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any, Self, override

import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeListChatModel, FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime

from omt.agent.agent import Agent
from omt.core.conversation import Conversation
from omt.core.events import RunEvent


class ToolModel(FakeMessagesListChatModel):
    @override
    def bind_tools(self, tools: Sequence[object], **kwargs: Any) -> Self:
        return self


class ReasoningModel(FakeListChatModel):
    def chunks(self) -> Iterator[ChatGenerationChunk]:
        for block in (
            {"type": "reasoning", "reasoning": "先", "index": 0},
            {"type": "reasoning", "reasoning": "分析", "index": 0},
            {"type": "text", "text": "答", "index": 1},
            {"type": "text", "text": "案", "index": 1},
        ):
            yield ChatGenerationChunk(message=AIMessageChunk(content=[block], id="reasoning-message"))

    @override
    def _stream(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None, **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        yield from self.chunks()

    @override
    async def _astream(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        for chunk in self.chunks():
            yield chunk


@pytest.mark.parametrize("asynchronous", [False, True])
def test_reasoning_deltas_are_ordered_separate_and_not_duplicated_at_finish(asynchronous: bool) -> None:
    received: list[RunEvent] = []
    conversation = Conversation(Agent(ReasoningModel(responses=["未使用"])), skill_roots=[], event_handler=received.append)
    answer = asyncio.run(conversation.arun("提问")) if asynchronous else conversation.run("提问")
    assert answer == "答案"
    assert [event.text for event in received if event.kind == "assistant.reasoning.delta"] == ["先", "分析"]
    assert [event.text for event in received if event.kind == "assistant.delta"] == ["答", "案"]
    assert [event.kind for event in received] == [
        "assistant.started", "assistant.reasoning.delta", "assistant.reasoning.delta",
        "assistant.delta", "assistant.delta", "assistant.completed",
    ]
    assert {event.message_id for event in received} == {conversation.messages[-1].id}
    assert [message.type for message in conversation.messages] == ["human", "ai"]
    final = conversation.messages[-1]
    assert isinstance(final, AIMessage)
    assert any(block["type"] == "reasoning" and block.get("reasoning") == "先分析" for block in final.content_blocks)


def test_parallel_tool_progress_is_preserved_when_second_finishes_before_first() -> None:
    async def execute() -> None:
        first_entered = asyncio.Event()
        second_finished = asyncio.Event()
        received: list[RunEvent] = []

        @tool
        async def parallel(which: str, runtime: ToolRuntime) -> str:
            """强制后启动的调用先输出并完成，不依赖调度运气。"""
            if which == "first":
                first_entered.set()
                await second_finished.wait()
                runtime.emit_output_delta("第一项进度")
            else:
                await first_entered.wait()
                runtime.emit_output_delta("第二项进度")
                second_finished.set()
            return which

        model = ToolModel(responses=[AIMessage(content="", tool_calls=[
            {"name": "parallel", "id": "first", "args": {"which": "first"}},
            {"name": "parallel", "id": "second", "args": {"which": "second"}},
        ]), AIMessage(content="完成")])
        conversation = Conversation(Agent(model, tools=[parallel]), skill_roots=[], event_handler=received.append)
        assert await asyncio.wait_for(conversation.arun("并行执行"), 5) == "完成"
        deltas = {event.tool_call_id: event.text or event.data for event in received if event.kind == "tool.delta"}
        assert deltas == {"first": "第一项进度", "second": "第二项进度"}
        assert {event.tool_call_id for event in received if event.kind == "tool.completed"} == {"first", "second"}
    asyncio.run(execute())


@pytest.mark.parametrize("asynchronous", [False, True])
def test_finish_only_reasoning_is_emitted_without_becoming_answer(asynchronous: bool) -> None:
    received: list[RunEvent] = []
    model = FakeMessagesListChatModel(responses=[AIMessage(content=[
        {"type": "reasoning", "reasoning": "仅提供最终思考块"}, {"type": "text", "text": "回答"},
    ])])
    conversation = Conversation(Agent(model), skill_roots=[], event_handler=received.append)
    answer = asyncio.run(conversation.arun("提问")) if asynchronous else conversation.run("提问")
    assert answer == "回答"
    assert "".join(event.text for event in received if event.kind == "assistant.reasoning.delta") == "仅提供最终思考块"
    assert "".join(event.text for event in received if event.kind == "assistant.delta") == "回答"
