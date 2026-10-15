"""同步 v3 事件流测试：本地模型和内存检查点，无网络请求。"""

from dataclasses import dataclass
from unittest.mock import Mock

import pytest
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.stream import GraphRunStream
from langgraph.types import Command

from omt.agent.agent import Agent


@pytest.mark.parametrize(
    "input_value", [{"messages": [{"role": "user", "content": "你好"}]}, Command(resume="继续"), None]
)
def test_stream_event_forwards_options_and_returns_original_stream(input_value):
    agent = Agent(FakeListChatModel(responses=["未使用"]))
    graph = Mock()
    agent.agent = graph
    config = {"configurable": {"thread_id": "local-stream"}}
    context = {"user_id": "local-user"}
    options = {
        "context": context,
        "version": "v3",
        "print_mode": "values",
        "output_keys": ["messages"],
        "interrupt_before": "*",
        "interrupt_after": ["tools"],
        "durability": "sync",
        "debug": True,
    }

    run = agent.stream_event(input_value, config, **options)

    assert run is graph.stream_events.return_value
    graph.stream_events.assert_called_once_with(input=input_value, config=config, **options)
    graph.astream_events.assert_not_called()
    assert graph.stream_events.call_args.kwargs["input"] is input_value
    assert graph.stream_events.call_args.kwargs["config"] is config
    assert graph.stream_events.call_args.kwargs["context"] is context


def test_stream_event_defaults_to_v3_and_is_lazy():
    model = FakeListChatModel(responses=["本地回复", "未使用"])
    agent = Agent(model)

    with agent.stream_event(
        {"messages": [{"role": "user", "content": "你好"}]}
    ) as run:
        assert isinstance(run, GraphRunStream)
        assert model.i == 0
        events = list(run)
        output = run.output

    assert events
    assert all("method" in event and "params" in event for event in events)
    assert any(event["method"] == "values" for event in events)
    assert output["messages"][-1].text == "本地回复"
    assert model.i == 1


def test_stream_event_values_projection_and_output():
    agent = Agent(FakeListChatModel(responses=["本地回复"]))

    with agent.stream_event(
        {"messages": [{"role": "user", "content": "你好"}]}
    ) as run:
        values = list(run.values)
        output = run.output

    assert values[-1]["messages"][-1].text == "本地回复"
    assert output["messages"][-1].text == "本地回复"


def test_stream_event_messages_projection_streams_text():
    agent = Agent(FakeListChatModel(responses=["本地回复"]))

    with agent.stream_event(
        {"messages": [{"role": "user", "content": "你好"}]}
    ) as run:
        chunks = []
        for message in run.messages:
            chunks.extend(message.text)
        assert run.output["messages"][-1].text == "本地回复"

    assert chunks == ["本", "地", "回", "复"]


def test_stream_event_context_manager_aborts_early():
    model = FakeListChatModel(responses=["不应执行", "未使用"])
    agent = Agent(model)

    with agent.stream_event(
        {"messages": [{"role": "user", "content": "你好"}]}
    ) as run:
        cursor = iter(run)
        first = next(cursor)
        assert first["method"] == "values"

    assert list(cursor) == []
    assert model.i == 0
    run.abort()  # 提前退出后再次关闭也应安全。


def test_stream_event_can_interrupt_and_resume_synchronously():
    agent = Agent(
        FakeListChatModel(responses=["恢复后的回复"]), checkpointer=InMemorySaver()
    )
    config = {"configurable": {"thread_id": "local-stream-interrupt"}}

    with agent.stream_event(
        {"messages": [{"role": "user", "content": "你好"}]},
        config,
        interrupt_before=["model"],
        durability="sync",
    ) as paused:
        assert len(paused.output["messages"]) == 1
        # 静态节点中断不携带动态 interrupt 的 payload。
        assert agent.agent.get_state(config).next == ("model",)

    with agent.stream_event(None, config, interrupt_before=[], durability="sync") as resumed:
        assert resumed.output["messages"][-1].text == "恢复后的回复"
        assert not resumed.interrupted


def test_stream_event_passes_context_to_middleware():
    @dataclass
    class Context:
        user_id: str

    contexts = []

    class CaptureContext(AgentMiddleware):
        def before_model(self, state, runtime):
            contexts.append(runtime.context)
            return None

    context = Context(user_id="local-user")
    agent = Agent(
        FakeListChatModel(responses=["本地回复"]),
        context_schema=Context,
        middleware=[CaptureContext()],
    )

    with agent.stream_event(
        {"messages": [{"role": "user", "content": "你好"}]}, context=context
    ) as run:
        assert run.output["messages"][-1].text == "本地回复"

    assert contexts == [context]
    assert contexts[0] is context


def test_stream_event_preserves_execution_error(monkeypatch: pytest.MonkeyPatch):
    error = RuntimeError("模型调用失败")
    monkeypatch.setattr(FakeListChatModel, "_stream", Mock(side_effect=error))
    agent = Agent(FakeListChatModel(responses=["未使用"]))

    with agent.stream_event({"messages": []}) as run:
        with pytest.raises(RuntimeError) as raised:
            list(run)

    assert raised.value is error


@pytest.mark.parametrize("version", ["v1", "v2", "unknown"])
def test_stream_event_rejects_other_protocols(version):
    agent = Agent(FakeListChatModel(responses=["未使用"]))
    agent.agent = Mock()

    with pytest.raises(ValueError, match="仅支持同步 v3"):
        agent.stream_event({"messages": []}, version=version)

    agent.agent.stream_events.assert_not_called()
