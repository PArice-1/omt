"""会话回归测试：真实 Agent 图、本地假模型和工具，不访问网络。"""

from copy import deepcopy
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import ToolRuntime
from pydantic import Field

from omt.agent.agent import Agent
from omt.coding_agent.runtime import CreateAgentSession


class LocalToolModel(FakeMessagesListChatModel):
    """允许绑定工具，并记录每次模型调用实际收到的完整上下文。"""

    seen_messages: list[list[BaseMessage]] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.seen_messages.append(list(messages))
        return super()._generate(messages, stop, run_manager, **kwargs)


def make_tool_session(*, intermediate_text: str = "我先读取说明。"):
    @tool
    def read_note(name: str, runtime: ToolRuntime) -> str:
        """读取本地测试说明。"""
        runtime.emit_output_delta("正在读取文件……")
        return f"文件内容：{name} 是项目说明。"

    model = LocalToolModel(
        responses=[
            AIMessage(
                content=intermediate_text,
                tool_calls=[
                    {
                        "name": "read_note",
                        "args": {"name": "README.md"},
                        "id": "read-note-001",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="第一轮总结完成。"),
            AIMessage(content="我记得上一轮的文件内容。"),
        ]
    )
    return CreateAgentSession(Agent(model, tools=[read_note]), config={}), model


@pytest.mark.parametrize("message", ["", "   ", "\n\t"])
def test_run_rejects_blank_input_without_changing_history(message):
    session = CreateAgentSession(Agent(FakeListChatModel(responses=["未使用"])), {})

    with pytest.raises(ValueError, match="message不能为空"):
        session.run(message)

    assert session.messages == []


def test_run_saves_user_tool_request_tool_result_and_final_ai_message():
    session, _ = make_tool_session()

    result = session.run("读取并总结说明")

    assert [message.type for message in session.messages] == [
        "human", "ai", "tool", "ai"
    ]
    assert result == "第一轮总结完成。"
    assert isinstance(session.messages[0], HumanMessage)
    request = session.messages[1]
    assert isinstance(request, AIMessage)
    assert request.tool_calls[0]["args"] == {"name": "README.md"}
    tool_result = session.messages[2]
    assert isinstance(tool_result, ToolMessage)
    assert tool_result.tool_call_id == request.tool_calls[0]["id"]
    assert tool_result.text == "文件内容：README.md 是项目说明。"
    assert session.messages[-1].text == "第一轮总结完成。"


def test_next_turn_receives_complete_previous_history_without_duplicates():
    session, model = make_tool_session()
    first_result = session.run("读取并总结说明")
    first_messages = list(session.messages)

    second_result = session.run("还记得刚才的内容吗？")

    assert [message.type for message in session.messages] == [
        "human", "ai", "tool", "ai", "human", "ai"
    ]
    assert model.seen_messages[-1][:-1] == first_messages
    assert model.seen_messages[-1][-1].text == "还记得刚才的内容吗？"
    assert session.messages[-1].text == "我记得上一轮的文件内容。"
    assert first_result == "第一轮总结完成。"
    assert second_result == "我记得上一轮的文件内容。"
    assert session.messages[:len(first_messages)] == first_messages
    assert len({message.id for message in session.messages}) == 6


def test_display_includes_intermediate_text_tool_input_progress_result_and_answer(capsys):
    session, _ = make_tool_session()

    session.run("读取并总结说明")

    output = capsys.readouterr().out
    expected = [
        "我先读取说明。",
        "[工具开始] read_note",
        '"name": "README.md"',
        "正在读取文件……",
        "文件内容：README.md 是项目说明。",
        "[工具完成] read_note",
        "第一轮总结完成。",
    ]
    positions = [output.index(text) for text in expected]
    assert positions == sorted(positions)
    for text in expected:
        assert output.count(text) == 1
    assert "additional_kwargs=" not in output
    assert "response_metadata=" not in output


def test_tool_only_model_message_is_saved_and_tool_execution_is_displayed(capsys):
    session, _ = make_tool_session(intermediate_text="")

    session.run("读取说明")

    output = capsys.readouterr().out
    assert "[工具开始] read_note" in output
    assert "第一轮总结完成。" in output
    request = session.messages[1]
    assert isinstance(request, AIMessage)
    assert request.text == ""
    assert request.tool_calls


def test_run_streams_plain_text_once_and_preserves_original_user_text(capsys):
    session = CreateAgentSession(
        Agent(FakeListChatModel(responses=["流式回答"])), config={}
    )

    result = session.run("  你好\n")

    assert session.messages[0].content == "  你好\n"
    assert result == "流式回答"
    assert capsys.readouterr().out.count("流式回答") == 1


def test_run_returns_plain_text_when_final_ai_content_is_a_block_list():
    model = FakeMessagesListChatModel(
        responses=[
            AIMessage(content=[
                {"type": "text", "text": "第一段"},
                {"type": "text", "text": "第二段"},
            ])
        ]
    )
    session = CreateAgentSession(Agent(model))

    reply = session.run("返回两段文字")

    assert reply == "第一段第二段"
    assert isinstance(session.messages[-1].content, list)


@pytest.mark.parametrize("config", [None, {}, {"configurable": {}}])
def test_session_generates_uuid_thread_id_when_missing(config: RunnableConfig | None):
    session = CreateAgentSession(Agent(FakeListChatModel(responses=["未使用"])), config)

    thread_id = session.config.get("configurable", {})["thread_id"]
    assert isinstance(thread_id, str)
    assert UUID(thread_id).version == 4
    assert str(UUID(thread_id)) == thread_id


def test_session_preserves_external_thread_id_and_other_config():
    config: RunnableConfig = {
        "configurable": {"thread_id": "existing-session", "workspace": "local-workspace"},
        "tags": ["local-test"],
        "metadata": {"source": "runtime-test"},
        "recursion_limit": 12,
    }
    before = deepcopy(config)

    session = CreateAgentSession(Agent(FakeListChatModel(responses=["未使用"])), config)

    assert session.config == before
    assert config == before
    assert session.config is not config
    assert session.config.get("configurable", {}) is not config["configurable"]


def test_generating_thread_id_does_not_modify_external_config():
    config: RunnableConfig = {
        "configurable": {"workspace": "original-workspace"},
        "tags": ["keep-tag"],
        "recursion_limit": 18,
    }
    before = deepcopy(config)

    session = CreateAgentSession(Agent(FakeListChatModel(responses=["未使用"])), config)

    assert config == before
    configurable = session.config.get("configurable", {})
    assert configurable["workspace"] == "original-workspace"
    assert session.config.get("tags") == ["keep-tag"]
    assert session.config.get("recursion_limit") == 18
    assert "thread_id" in configurable
    configurable["workspace"] = "session-workspace"
    assert config["configurable"]["workspace"] == "original-workspace"


def test_new_sessions_have_different_thread_ids():
    agent = Agent(FakeListChatModel(responses=["未使用"]))

    first = CreateAgentSession(agent)
    second = CreateAgentSession(agent)

    first_id = first.config.get("configurable", {})["thread_id"]
    second_id = second.config.get("configurable", {})["thread_id"]
    assert first_id != second_id


def test_run_reuses_and_forwards_session_thread_id(monkeypatch: pytest.MonkeyPatch):
    agent = Agent(FakeListChatModel(responses=["第一轮回答", "第二轮回答"]))
    stream_event = Mock(wraps=agent.stream_event)
    monkeypatch.setattr(agent, "stream_event", stream_event)
    session = CreateAgentSession(agent)
    thread_id = session.config.get("configurable", {})["thread_id"]

    assert session.run("第一轮") == "第一轮回答"
    assert session.run("第二轮") == "第二轮回答"

    assert session.config.get("configurable", {})["thread_id"] == thread_id
    assert stream_event.call_count == 2
    for call in stream_event.call_args_list:
        assert call.kwargs["config"] is session.config
        assert call.kwargs["config"]["configurable"]["thread_id"] == thread_id


def test_generated_thread_id_works_with_checkpointer_without_duplicate_history():
    agent = Agent(
        FakeListChatModel(responses=["第一轮回答", "第二轮回答"]),
        checkpointer=InMemorySaver(),
    )
    session = CreateAgentSession(agent)

    session.run("第一轮")
    session.run("第二轮")

    assert [message.text for message in session.messages] == [
        "第一轮", "第一轮回答", "第二轮", "第二轮回答"
    ]
    assert agent.agent.get_state(session.config).values["messages"] == session.messages


def test_config_is_optional():
    session = CreateAgentSession(Agent(FakeListChatModel(responses=["你好"])))

    session.run("你好")

    assert [message.type for message in session.messages] == ["human", "ai"]


def test_failed_turn_preserves_previous_history_and_propagates_error(monkeypatch, capsys):
    session = CreateAgentSession(
        Agent(FakeListChatModel(responses=["第一轮回答"])), config={}
    )
    session.run("第一轮")
    before = list(session.messages)
    error = RuntimeError("本地模型失败")
    monkeypatch.setattr(FakeListChatModel, "_stream", Mock(side_effect=error))

    with pytest.raises(RuntimeError) as raised:
        session.run("失败的一轮")

    assert raised.value is error
    assert session.messages == before
    assert "[执行失败] 本地模型失败" in capsys.readouterr().out


def test_tool_exception_is_displayed_and_not_saved_as_a_successful_turn(capsys):
    @tool
    def fail_tool() -> str:
        """模拟本地工具执行失败。"""
        raise RuntimeError("本地工具失败")

    model = LocalToolModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "fail_tool", "args": {}, "id": "failed-call"}
                ],
            )
        ]
    )
    session = CreateAgentSession(Agent(model, tools=[fail_tool]), config={})

    with pytest.raises(RuntimeError, match="本地工具失败"):
        session.run("执行失败工具")

    output = capsys.readouterr().out
    assert "[工具失败] fail_tool" in output
    assert "本地工具失败" in output
    assert "[工具完成] fail_tool" not in output
    assert session.messages == []


def test_error_tool_message_is_displayed_as_failure_and_kept_in_history(capsys):
    @tool
    def error_result(runtime: ToolRuntime) -> ToolMessage:
        """返回已经处理的工具错误，让模型继续解释。"""
        return ToolMessage(
            content="文件不存在",
            tool_call_id=runtime.tool_call_id or "",
            status="error",
        )

    model = LocalToolModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "error_result", "args": {}, "id": "handled-error"}
                ],
            ),
            AIMessage(content="文件不存在，请检查路径。"),
        ]
    )
    session = CreateAgentSession(Agent(model, tools=[error_result]), config={})

    session.run("读取不存在的文件")

    output = capsys.readouterr().out
    assert "[工具失败] error_result" in output
    assert "[工具完成] error_result" not in output
    tool_result = session.messages[2]
    assert isinstance(tool_result, ToolMessage)
    assert tool_result.status == "error"
    assert session.messages[-1].text == "文件不存在，请检查路径。"


def test_multiple_tool_calls_are_saved_with_their_matching_results():
    @tool
    def echo_note(name: str) -> str:
        """返回指定测试文件名，无增量输出。"""
        return f"内容：{name}"

    model = LocalToolModel(
        responses=[
            AIMessage(
                content="需要看两个文件。",
                tool_calls=[
                    {"name": "echo_note", "args": {"name": "a.md"}, "id": "call-a"},
                    {"name": "echo_note", "args": {"name": "b.md"}, "id": "call-b"},
                ],
            ),
            AIMessage(content="两个文件都已读取。"),
        ]
    )
    session = CreateAgentSession(Agent(model, tools=[echo_note]))

    session.run("读取两个文件")

    tool_messages = [msg for msg in session.messages if isinstance(msg, ToolMessage)]
    assert {msg.tool_call_id: msg.text for msg in tool_messages} == {
        "call-a": "内容：a.md",
        "call-b": "内容：b.md",
    }
    assert len(session.messages) == 5
    assert session.messages[-1].text == "两个文件都已读取。"


def test_tool_artifact_and_ai_metadata_are_preserved_in_history(capsys):
    artifact = {"source": "本地测试", "raw": [1, 2, 3]}

    @tool(response_format="content_and_artifact")
    def load_artifact() -> tuple[str, dict[str, Any]]:
        """返回可显示的文本和不应丢失的原始数据。"""
        return "可显示的工具结果", artifact

    model = LocalToolModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "load_artifact", "args": {}, "id": "artifact"}],
                additional_kwargs={"custom_request": "保留请求附加信息"},
            ),
            AIMessage(
                content="最后的回答",
                response_metadata={"finish_reason": "stop"},
                usage_metadata={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
            ),
        ]
    )
    session = CreateAgentSession(Agent(model, tools=[load_artifact]))

    session.run("读取原始数据")

    request = session.messages[1]
    assert request.additional_kwargs["custom_request"] == "保留请求附加信息"
    tool_message = session.messages[2]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.artifact == artifact
    final_message = session.messages[-1]
    assert isinstance(final_message, AIMessage)
    assert final_message.response_metadata["finish_reason"] == "stop"
    assert final_message.usage_metadata is not None
    assert final_message.usage_metadata["total_tokens"] == 8
    output = capsys.readouterr().out
    assert "可显示的工具结果" in output
    assert "response_metadata=" not in output
    assert "custom_request" not in output


def test_sessions_do_not_share_message_history():
    first = CreateAgentSession(Agent(FakeListChatModel(responses=["第一会话回复"])))
    second = CreateAgentSession(Agent(FakeListChatModel(responses=["第二会话回复"])))

    first.run("第一会话问题")
    second.run("第二会话问题")

    assert [message.text for message in first.messages] == ["第一会话问题", "第一会话回复"]
    assert [message.text for message in second.messages] == ["第二会话问题", "第二会话回复"]


@pytest.mark.parametrize(
    "incomplete_messages",
    [
        [],
        [ToolMessage(content="只是工具结果", tool_call_id="unfinished")],
        [AIMessage(content="准备调用工具", tool_calls=[
            {"name": "read", "args": {}, "id": "unfinished"}
        ])],
    ],
)
def test_run_does_not_return_tool_output_or_pending_request_as_final_answer(
    monkeypatch, incomplete_messages
):
    session = CreateAgentSession(Agent(FakeListChatModel(responses=["已完成的回答"])))
    session.run("第一轮")
    before = list(session.messages)
    monkeypatch.setattr(
        session,
        "_display_agent_run",
        Mock(return_value={"messages": incomplete_messages}),
    )

    with pytest.raises(RuntimeError, match="未返回最终 AIMessage"):
        session.run("未完成的下一轮")

    assert session.messages == before


def test_missing_final_state_does_not_commit_user_message(monkeypatch):
    agent = Agent(FakeListChatModel(responses=["未使用"]))
    stream = Mock()
    stream.interleave.return_value = iter(())
    stream.output = None
    context = Mock()
    context.__enter__ = Mock(return_value=stream)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(agent, "stream_event", Mock(return_value=context))
    session = CreateAgentSession(agent, config={})

    with pytest.raises(RuntimeError, match="未返回最终状态"):
        session.run("不要丢失最终状态")

    assert session.messages == []
