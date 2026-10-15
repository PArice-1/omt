"""Agent 封装测试：使用本地假模型和调用替身，不访问网络或凭据文件。"""

from dataclasses import dataclass
from importlib import import_module
from inspect import signature
from unittest.mock import Mock

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain.agents.structured_output import ProviderStrategy, ToolStrategy
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from pydantic import BaseModel

from omt.agent.agent import Agent


def test_invoke_runs_real_graph_with_local_model():
    agent = Agent(FakeListChatModel(responses=["本地回复"]), tools=[])

    result = agent.invoke({"messages": [{"role": "user", "content": "你好"}]})

    assert len(result["messages"]) == 2
    assert isinstance(result["messages"][-1], AIMessage)
    assert result["messages"][-1].content == "本地回复"


def test_constructor_defaults_to_no_tools():
    agent = Agent(FakeListChatModel(responses=["无需工具"]))

    assert agent.tools is None
    result = agent.invoke({"messages": [{"role": "user", "content": "你好"}]})
    assert result["messages"][-1].content == "无需工具"


@pytest.mark.parametrize(
    "input_value",
    [
        {"messages": [{"role": "user", "content": "你好"}], "custom_field": 1},
        Command(resume="继续"),
        None,
    ],
)
def test_invoke_forwards_input_config_context_and_extra_options(input_value):
    @dataclass
    class Context:
        user_id: str

    agent = Agent(FakeListChatModel(responses=["未使用"]), tools=[])
    graph = Mock()
    agent.agent = graph
    config = {"configurable": {"thread_id": "local-thread"}}
    context = Context(user_id="local-user")

    result = agent.invoke(
        input_value,
        config,
        context=context,
        stream_mode="updates",
        print_mode="values",
        output_keys=["messages"],
        interrupt_before="*",
        interrupt_after=["tools"],
        durability="sync",
        version="v2",
    )

    assert result is graph.invoke.return_value
    graph.invoke.assert_called_once_with(
        input=input_value,
        config=config,
        context=context,
        stream_mode="updates",
        print_mode="values",
        output_keys=["messages"],
        interrupt_before="*",
        interrupt_after=["tools"],
        durability="sync",
        version="v2",
    )
    assert graph.invoke.call_args.kwargs["input"] is input_value
    assert graph.invoke.call_args.kwargs["config"] is config
    assert graph.invoke.call_args.kwargs["context"] is context


def test_invoke_preserves_graph_exception():
    agent = Agent(FakeListChatModel(responses=["未使用"]), tools=[])
    error = RuntimeError("图调用失败")
    agent.agent = Mock()
    agent.agent.invoke.side_effect = error

    with pytest.raises(RuntimeError) as raised:
        agent.invoke({"messages": []})

    assert raised.value is error


class StructuredReply(BaseModel):
    text: str


@pytest.mark.parametrize(
    "response_format",
    [
        StructuredReply,
        StructuredReply.model_json_schema(),
        ToolStrategy(StructuredReply),
        ProviderStrategy(StructuredReply),
    ],
)
def test_constructor_forwards_explicit_options_and_extra_options(
    monkeypatch: pytest.MonkeyPatch, response_format
):
    @dataclass
    class Context:
        user_id: str

    class State(AgentState):
        custom_field: int

    def lookup(value: str) -> str:
        """返回本地输入。"""
        return value

    factory = Mock()
    monkeypatch.setattr(import_module("omt.agent.agent"), "create_agent", factory)
    tools = (lookup, {"type": "function", "function": {"name": "lookup"}})
    options = {
        "system_prompt": SystemMessage(content="你是本地助手"),
        "middleware": (AgentMiddleware(),),
        "response_format": response_format,
        "state_schema": State,
        "context_schema": Context,
        "checkpointer": InMemorySaver(),
        "store": InMemoryStore(),
        "interrupt_before": ["tools"],
        "interrupt_after": ["model"],
        "debug": True,
        "name": "local-agent",
        "cache": None,
    }

    agent = Agent("deepseek:deepseek-chat", tools, **options)

    factory.assert_called_once_with(model="deepseek:deepseek-chat", tools=tools, **options)
    assert agent.model == "deepseek:deepseek-chat"
    assert agent.tools is tools
    assert agent.agent is factory.return_value
    for key, value in options.items():
        assert factory.call_args.kwargs[key] is value


def test_explicit_defaults_match_underlying_functions():
    constructor_parameters = signature(Agent.__init__).parameters
    factory_parameters = signature(create_agent).parameters
    invoke_parameters = signature(Agent.invoke).parameters
    graph_parameters = signature(CompiledStateGraph.invoke).parameters

    for name, parameter in constructor_parameters.items():
        if name not in {"self", "kwargs"}:
            assert parameter.default == factory_parameters[name].default
            assert parameter.kind == factory_parameters[name].kind
    for name, parameter in invoke_parameters.items():
        if name not in {"self", "kwargs"}:
            assert parameter.default == graph_parameters[name].default
            assert parameter.kind == graph_parameters[name].kind


def test_checkpointer_keeps_conversation_history():
    agent = Agent(
        FakeListChatModel(responses=["第一轮回复", "第二轮回复"]),
        system_prompt="你是本地助手",
        checkpointer=InMemorySaver(),
        name="local-agent",
    )
    config = {"configurable": {"thread_id": "local-conversation"}}

    first = agent.invoke(
        {"messages": [{"role": "user", "content": "第一轮"}]},
        config,
        durability="sync",
    )
    second = agent.invoke(
        {"messages": [{"role": "user", "content": "第二轮"}]},
        config,
        durability="sync",
    )

    assert len(first["messages"]) == 2
    assert [message.content for message in second["messages"]] == [
        "第一轮", "第一轮回复", "第二轮", "第二轮回复"
    ]


def test_context_schema_and_middleware_receive_runtime_context():
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
        middleware=[CaptureContext()],
        context_schema=Context,
    )

    result = agent.invoke(
        {"messages": [{"role": "user", "content": "你好"}]}, context=context
    )

    assert result["messages"][-1].content == "本地回复"
    assert contexts == [context]
    assert contexts[0] is context


def test_invoke_updates_mode_returns_collected_updates():
    agent = Agent(FakeListChatModel(responses=["本地回复"]))

    updates = agent.invoke(
        {"messages": [{"role": "user", "content": "你好"}]}, stream_mode="updates"
    )

    assert isinstance(updates, list)
    assert updates[-1]["model"]["messages"][-1].content == "本地回复"


def test_invoke_output_keys_selects_messages():
    agent = Agent(FakeListChatModel(responses=["本地回复"]))

    messages = agent.invoke(
        {"messages": [{"role": "user", "content": "你好"}]}, output_keys="messages"
    )

    assert isinstance(messages, list)
    assert messages[-1].content == "本地回复"


def test_interrupt_before_model_can_resume_with_none():
    agent = Agent(
        FakeListChatModel(responses=["恢复后的回复"]),
        checkpointer=InMemorySaver(),
        interrupt_before=["model"],
    )
    config = {"configurable": {"thread_id": "local-interrupt"}}

    paused = agent.invoke(
        {"messages": [{"role": "user", "content": "你好"}]}, config
    )
    resumed = agent.invoke(None, config, interrupt_before=[])

    assert len(paused["messages"]) == 1
    assert len(resumed["messages"]) == 2
    assert resumed["messages"][-1].content == "恢复后的回复"
