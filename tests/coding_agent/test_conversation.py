"""会话回归测试：真实 Agent 图、本地假模型和工具，不访问网络。"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeMessagesListChatModel,
)
from langchain_core.messages import (
    AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage, ToolMessage,
)
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import ToolRuntime
from pydantic import Field

from omt.agent.agent import Agent
from omt.coding_agent import settings as settings_module
from omt.coding_agent.conversation import Conversation
from omt.cli.terminal import print_event
from omt.coding_agent.events import EventHandler, RunEvent
from omt.coding_agent.tools.read import read


def make_conversation(
    agent: Agent, config: RunnableConfig | None = None, *, system_prompt: str = "",
    skill_roots: Sequence[Path] | None = None, event_handler: EventHandler | None = print_event,
) -> Conversation:
    """原有展示回归显式安装 CLI 渲染，核心默认静默另有用例验证。"""
    return Conversation(
        agent, config, system_prompt=system_prompt, skill_roots=skill_roots,
        event_handler=event_handler,
    )


@pytest.fixture(autouse=True)
def isolated_runtime_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """隔离默认 Skills 的用户目录与项目目录，不读取本机文件。"""
    monkeypatch.setattr(settings_module, "root", tmp_path / ".omt")
    monkeypatch.chdir(tmp_path)


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
    return make_conversation(Agent(model, tools=[read_note]), config={}), model


@pytest.mark.parametrize("message", ["", "   ", "\n\t"])
def test_run_rejects_blank_input_without_changing_history(message):
    session = make_conversation(Agent(FakeListChatModel(responses=["未使用"])), {})

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
    session = make_conversation(
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
    session = make_conversation(Agent(model))

    reply = session.run("返回两段文字")

    assert reply == "第一段第二段"
    assert isinstance(session.messages[-1].content, list)


@pytest.mark.parametrize("config", [None, {}, {"configurable": {}}])
def test_session_generates_uuid_thread_id_when_missing(config: RunnableConfig | None):
    session = make_conversation(Agent(FakeListChatModel(responses=["未使用"])), config)

    thread_id = session.config.get("configurable", {})["thread_id"]
    assert isinstance(thread_id, str)
    assert UUID(thread_id).version == 4
    assert str(UUID(thread_id)) == thread_id


def test_session_preserves_external_thread_id_and_other_config() -> None:
    config: RunnableConfig = {
        "configurable": {"thread_id": "existing-session", "workspace": "local-workspace"},
        "tags": ["local-test"],
        "metadata": {"source": "runtime-test"},
        "recursion_limit": 12,
    }
    before = deepcopy(config)

    session = make_conversation(Agent(FakeListChatModel(responses=["未使用"])), config)

    assert session.config == before
    assert session.get_config() == before
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

    session = make_conversation(Agent(FakeListChatModel(responses=["未使用"])), config)

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

    first = make_conversation(agent)
    second = make_conversation(agent)

    first_id = first.config.get("configurable", {})["thread_id"]
    second_id = second.config.get("configurable", {})["thread_id"]
    assert first_id != second_id


def test_run_reuses_and_forwards_session_thread_id(monkeypatch: pytest.MonkeyPatch):
    agent = Agent(FakeListChatModel(responses=["第一轮回答", "第二轮回答"]))
    stream_event = Mock(wraps=agent.stream_event)
    monkeypatch.setattr(agent, "stream_event", stream_event)
    session = make_conversation(agent)
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
    session = make_conversation(agent)

    session.run("第一轮")
    session.run("第二轮")

    assert [message.text for message in session.messages] == [
        "第一轮", "第一轮回答", "第二轮", "第二轮回答"
    ]
    assert agent.agent.get_state(session.config).values["messages"] == session.messages


def test_config_is_optional():
    session = make_conversation(Agent(FakeListChatModel(responses=["你好"])))

    session.run("你好")

    assert [message.type for message in session.messages] == ["human", "ai"]


def test_failed_turn_preserves_previous_history_and_propagates_error(monkeypatch, capsys):
    session = make_conversation(
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
    session = make_conversation(Agent(model, tools=[fail_tool]), config={})

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
    session = make_conversation(Agent(model, tools=[error_result]), config={})

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
    session = make_conversation(Agent(model, tools=[echo_note]))

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
    session = make_conversation(Agent(model, tools=[load_artifact]))

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
    first = make_conversation(Agent(FakeListChatModel(responses=["第一会话回复"])))
    second = make_conversation(Agent(FakeListChatModel(responses=["第二会话回复"])))

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
    session = make_conversation(Agent(FakeListChatModel(responses=["已完成的回答"])))
    session.run("第一轮")
    before = list(session.messages)
    monkeypatch.setattr(
        session,
        "_execute",
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
    session = make_conversation(agent, config={})

    with pytest.raises(RuntimeError, match="未返回最终状态"):
        session.run("不要丢失最终状态")

    assert session.messages == []


def _write_runtime_skill(root: Path, description: str = "审查代码") -> Path:
    file = root / "review" / "SKILL.md"
    file.parent.mkdir(parents=True, exist_ok=True)
    content = f"---\nname: review\ndescription: {description}\n---\n私有正文\n"
    _ = file.write_text(content, encoding="utf-8")
    return file


@pytest.mark.parametrize("prompt", ["", " \n\t"])
def test_initial_history_omits_empty_system_messages(prompt: str) -> None:
    session = make_conversation(
        Agent(FakeListChatModel(responses=["未使用"])),
        system_prompt=prompt,
        skill_roots=[],
    )

    assert session.messages == []


def test_external_system_prompt_is_preserved_in_initial_history() -> None:
    prompt = "  你是编码助手。\n保留提示词的原始格式。\n"
    session = make_conversation(
        Agent(FakeListChatModel(responses=["未使用"])),
        system_prompt=prompt,
        skill_roots=[],
    )

    assert len(session.messages) == 1
    assert isinstance(session.messages[0], SystemMessage)
    assert session.messages[0].content == prompt


def test_initial_history_combines_external_prompt_and_skill_catalog(
    tmp_path: Path,
) -> None:
    root = tmp_path / "custom"
    file = _write_runtime_skill(root)
    session = make_conversation(
        Agent(FakeListChatModel(responses=["未使用"])),
        system_prompt="基础规则",
        skill_roots=[root],
    )

    assert len(session.messages) == 1
    message = session.messages[0]
    assert isinstance(message, SystemMessage)
    assert message.text.startswith("基础规则\n\n## 可用 Skills")
    assert str(file.resolve()) in message.text
    assert "私有正文" not in message.text
    assert "limit=false" in message.text


def test_default_skill_roots_prefer_project_over_user(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    user = _write_runtime_skill(settings_module.root / "agent" / "skills", "用户版本")
    project = _write_runtime_skill(tmp_path / "skills", "项目版本")

    session = make_conversation(Agent(FakeListChatModel(responses=["未使用"])))

    assert len(session.messages) == 1
    assert isinstance(session.messages[0], SystemMessage)
    assert "项目版本" in session.messages[0].text
    assert "用户版本" not in session.messages[0].text
    assert str(project.resolve()) in session.messages[0].text
    diagnostic = capsys.readouterr().err
    assert "[Skills]" in diagnostic
    assert str(user) in diagnostic
    assert str(project) in diagnostic


def test_empty_skill_roots_disable_default_discovery(tmp_path: Path) -> None:
    _ = _write_runtime_skill(settings_module.root / "agent" / "skills")
    _ = _write_runtime_skill(tmp_path / "skills")

    session = make_conversation(
        Agent(FakeListChatModel(responses=["未使用"])), skill_roots=[]
    )

    assert session.messages == []


def test_explicit_skill_roots_replace_default_search_paths(tmp_path: Path) -> None:
    _ = _write_runtime_skill(settings_module.root / "agent" / "skills", "默认用户目录")
    _ = _write_runtime_skill(tmp_path / "skills", "默认项目目录")
    custom = tmp_path / "custom"
    file = _write_runtime_skill(custom, "显式目录")

    session = make_conversation(
        Agent(FakeListChatModel(responses=["未使用"])), skill_roots=[custom]
    )

    assert len(session.messages) == 1
    assert str(file.resolve()) in session.messages[0].text
    assert "显式目录" in session.messages[0].text
    assert "默认用户目录" not in session.messages[0].text
    assert "默认项目目录" not in session.messages[0].text


def test_invalid_skill_is_diagnosed_without_creating_an_empty_system_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = _write_runtime_skill(tmp_path / "bad")
    _ = file.write_text("---\nname: review\n---\nPRIVATE_BODY\n", encoding="utf-8")

    session = make_conversation(
        Agent(FakeListChatModel(responses=["未使用"])),
        skill_roots=[tmp_path / "bad"],
    )

    assert session.messages == []
    output = capsys.readouterr()
    assert output.out == ""
    assert str(file) in output.err
    assert "description" in output.err
    assert "PRIVATE_BODY" not in output.err


@pytest.mark.parametrize("with_checkpointer", [False, True])
def test_system_message_and_skill_read_survive_multiple_turns_without_duplicates(
    tmp_path: Path, with_checkpointer: bool
) -> None:
    root = tmp_path / "custom"
    file = _write_runtime_skill(root)
    content = file.read_text(encoding="utf-8") + "步骤\n" * 60 + "末尾指引\n"
    _ = file.write_text(content, encoding="utf-8")
    model = LocalToolModel(responses=[
        AIMessage(content="", tool_calls=[{
            "name": "read", "args": {"file": str(file), "limit": False},
            "id": "skill-read",
        }]),
        AIMessage(content="已读取指引。"),
        AIMessage(content="继续执行。"),
    ])
    agent = Agent(
        model,
        tools=[read],
        checkpointer=InMemorySaver() if with_checkpointer else None,
    )
    session = make_conversation(agent, system_prompt="基础规则", skill_roots=[root])
    initial_prompt = session.messages[0].text

    assert session.run("按指引检查") == "已读取指引。"
    # 会话中不自动重扫或热重载；已加载的目录与正文继续留在历史里。
    file.unlink()
    assert session.run("继续") == "继续执行。"

    assert [message.type for message in session.messages] == [
        "system", "human", "ai", "tool", "ai", "human", "ai"
    ]
    assert session.messages[0].text == initial_prompt
    assert session.messages[3].text == content
    assert all(messages[0].text == initial_prompt for messages in model.seen_messages)
    assert all(
        sum(isinstance(message, SystemMessage) for message in messages) == 1
        for messages in model.seen_messages
    )
    assert model.seen_messages[-1][:-1] == session.messages[:-2]
    if with_checkpointer:
        assert agent.agent.get_state(session.config).values["messages"] == session.messages


def test_failed_turn_preserves_initialized_system_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = LocalToolModel(responses=[AIMessage(content="完成")])
    session = make_conversation(Agent(model), system_prompt="基础规则", skill_roots=[])
    before = list(session.messages)

    with pytest.raises(ValueError, match="message不能为空"):
        _ = session.run(" ")
    assert session.messages == before

    monkeypatch.setattr(LocalToolModel, "_generate", Mock(side_effect=RuntimeError("失败")))
    with pytest.raises(RuntimeError, match="失败"):
        _ = session.run("执行")
    assert session.messages == before


@pytest.mark.parametrize("with_checkpointer", [False, True])
def test_arun_executes_mixed_tools_and_preserves_multiturn_history(
    with_checkpointer: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    @tool(response_format="content_and_artifact")
    async def async_note(runtime: ToolRuntime) -> tuple[str, dict[str, str]]:
        """异步读取本地说明并报告进度。"""
        runtime.emit_output_delta("异步读取中")
        return "异步说明", {"source": "本地假数据"}

    @tool
    def sync_note() -> str:
        """同步读取本地说明。"""
        return "同步说明"

    model = LocalToolModel(responses=[
        AIMessage(content="先读取说明。", tool_calls=[
            {"name": "async_note", "args": {}, "id": "async-note"},
            {"name": "sync_note", "args": {}, "id": "sync-note"},
        ]),
        AIMessage(
            content=[{"type": "text", "text": "第一轮完成。"}],
            response_metadata={"finish_reason": "stop"},
        ),
        AIMessage(content="记得之前的说明。"),
    ])
    agent = Agent(
        model, tools=[async_note, sync_note],
        checkpointer=InMemorySaver() if with_checkpointer else None,
    )
    session = make_conversation(agent, system_prompt="基础规则", skill_roots=[])
    thread_id = session.config.get("configurable", {})["thread_id"]

    async def execute() -> None:
        assert await session.arun("读取说明") == "第一轮完成。"
        before = list(session.messages)
        assert await session.arun("继续") == "记得之前的说明。"
        assert model.seen_messages[-1][:-1] == before
        if with_checkpointer:
            state = await agent.agent.aget_state(session.config)
            assert state.values["messages"] == session.messages

    asyncio.run(execute())

    assert [msg.type for msg in session.messages] == [
        "system", "human", "ai", "tool", "tool", "ai", "human", "ai"
    ]
    results = {msg.tool_call_id: msg for msg in session.messages if isinstance(msg, ToolMessage)}
    assert results["async-note"].text == "异步说明"
    assert results["async-note"].artifact == {"source": "本地假数据"}
    assert results["sync-note"].text == "同步说明"
    assert session.messages[5].response_metadata["finish_reason"] == "stop"
    assert session.config.get("configurable", {})["thread_id"] == thread_id
    assert all(sum(isinstance(msg, SystemMessage) for msg in messages) == 1
               for messages in model.seen_messages)
    output = capsys.readouterr().out
    for text in ("先读取说明。", "[工具开始] async_note", "异步读取中",
                 "[工具完成] async_note", "[工具完成] sync_note", "第一轮完成。"):
        assert output.count(text) == 1


def test_arun_streams_text_once_and_preserves_original_input(
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = make_conversation(Agent(FakeListChatModel(responses=["异步流式回答"])))

    assert asyncio.run(session.arun("  你好\n")) == "异步流式回答"

    assert session.messages[0].text == "  你好\n"
    assert capsys.readouterr().out.count("异步流式回答") == 1


@pytest.mark.parametrize("message", ["", " ", "\n\t"])
def test_arun_rejects_blank_input_without_changing_history(message: str) -> None:
    session = make_conversation(
        Agent(FakeListChatModel(responses=["未使用"])), system_prompt="基础规则"
    )
    before = list(session.messages)
    with pytest.raises(ValueError, match="message不能为空"):
        asyncio.run(session.arun(message))
    assert session.messages == before


def test_arun_failure_preserves_previous_history_and_original_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = make_conversation(Agent(FakeListChatModel(responses=["已完成"])))
    asyncio.run(session.arun("第一轮"))
    before = list(session.messages)
    error = RuntimeError("异步模型失败")

    async def fail_stream(
        self: FakeListChatModel, *args: object, **kwargs: object
    ) -> AsyncIterator[ChatGenerationChunk]:
        yield ChatGenerationChunk(message=AIMessageChunk(content="部分回答"))
        raise error

    monkeypatch.setattr(FakeListChatModel, "_astream", fail_stream)

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(session.arun("失败的一轮"))

    assert raised.value is error
    assert session.messages == before
    assert "[执行失败] 异步模型失败" in capsys.readouterr().out


@pytest.mark.parametrize("handled", [False, True])
def test_arun_displays_tool_failures_without_reporting_success(
    handled: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    error = RuntimeError("异步工具失败")

    @tool
    async def fail_note(runtime: ToolRuntime) -> ToolMessage:
        """返回已处理错误或抛出原始错误。"""
        if not handled:
            raise error
        return ToolMessage(
            content="说明不存在", tool_call_id=runtime.tool_call_id or "", status="error"
        )

    model = LocalToolModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "fail_note", "args": {}, "id": "fail"}]),
        AIMessage(content="请检查说明路径。"),
    ])
    session = make_conversation(Agent(model, tools=[fail_note]))
    if handled:
        assert asyncio.run(session.arun("读取说明")) == "请检查说明路径。"
        assert isinstance(session.messages[2], ToolMessage)
        assert session.messages[2].status == "error"
    else:
        with pytest.raises(RuntimeError) as raised:
            asyncio.run(session.arun("读取说明"))
        assert raised.value is error
        assert session.messages == []
    output = capsys.readouterr().out
    assert "[工具失败] fail_note" in output
    assert "[工具完成] fail_note" not in output


@pytest.mark.parametrize("result", [
    None, {"messages": []},
    {"messages": [ToolMessage(content="工具结果", tool_call_id="unfinished")]},
    {"messages": [AIMessage(content="准备执行", tool_calls=[
        {"name": "read", "args": {}, "id": "unfinished"}
    ])]},
])
def test_arun_incomplete_final_state_does_not_commit_history(
    monkeypatch: pytest.MonkeyPatch, result
) -> None:
    session = make_conversation(Agent(FakeListChatModel(responses=["已完成"])))
    asyncio.run(session.arun("第一轮"))
    before = list(session.messages)
    monkeypatch.setattr(session, "_aexecute", AsyncMock(return_value=result))

    with pytest.raises(RuntimeError, match="未返回最终"):
        asyncio.run(session.arun("未完成的一轮"))

    assert session.messages == before


def test_arun_cancellation_cleans_up_tool_and_consumers_without_committing_history(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def execute() -> None:
        started = asyncio.Event()
        cleaned = asyncio.Event()

        @tool
        async def wait_note() -> str:
            """等待取消，退出时清理本地任务。"""
            started.set()
            try:
                await asyncio.Event().wait()
                return "不应完成"
            finally:
                cleaned.set()

        model = LocalToolModel(responses=[AIMessage(content="", tool_calls=[
            {"name": "wait_note", "args": {}, "id": "wait"}
        ])])
        session = make_conversation(Agent(model, tools=[wait_note]), system_prompt="基础规则")
        before = list(session.messages)
        existing = asyncio.all_tasks()
        task = asyncio.create_task(session.arun("等待说明"))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert cleaned.is_set()
        assert session.messages == before
        assert not (asyncio.all_tasks() - existing)

    asyncio.run(execute())
    assert "[执行失败]" not in capsys.readouterr().out


def test_sync_and_async_turns_share_history() -> None:
    session = make_conversation(Agent(FakeListChatModel(responses=["同步一", "异步二", "同步三"])))

    assert session.run("一") == "同步一"
    assert asyncio.run(session.arun("二")) == "异步二"
    assert session.run("三") == "同步三"
    assert [msg.text for msg in session.messages] == ["一", "同步一", "二", "异步二", "三", "同步三"]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("collect_events", [False, True])
def test_execution_can_emit_events_without_terminal_output(
    asynchronous: bool, collect_events: bool, capsys: pytest.CaptureFixture[str],
) -> None:
    original, model = make_tool_session()
    events: list[RunEvent] = []
    session = make_conversation(
        original.agent, skill_roots=[], event_handler=events.append if collect_events else None,
    )

    answer = asyncio.run(session.arun("读取说明")) if asynchronous else session.run("读取说明")

    assert answer == "第一轮总结完成。"
    assert [message.type for message in session.messages] == ["human", "ai", "tool", "ai"]
    assert model.seen_messages
    assert capsys.readouterr().out == ""
    if collect_events:
        assert "".join(event.text for event in events if event.kind == "assistant.delta") == "我先读取说明。第一轮总结完成。"
        assert [event.kind for event in events if event.kind.startswith("tool.")] == [
            "tool.started", "tool.delta", "tool.completed",
        ]
        assert next(event for event in events if event.kind == "tool.started").data == {"name": "README.md"}
    else:
        assert events == []


def test_load_history_can_restore_without_displaying_history(capsys: pytest.CaptureFixture[str]) -> None:
    async def execute() -> None:
        agent = Agent(FakeListChatModel(responses=["已完成"]), checkpointer=InMemorySaver())
        source = make_conversation(
            agent, config={"configurable": {"thread_id": "quiet-source"}},
            skill_roots=[], event_handler=None,
        )
        assert await source.arun("原问题") == "已完成"
        target = make_conversation(agent, skill_roots=[], event_handler=None)
        await target.load_history("quiet-source")
        assert target.messages == source.messages
    asyncio.run(execute())
    assert capsys.readouterr().out == ""


def test_conversation_is_silent_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    invalid = tmp_path / "broken"
    invalid.mkdir()
    _ = (invalid / "SKILL.md").write_text("---\nname: broken\n---\n测试正文", encoding="utf-8")
    conversation = Conversation(Agent(FakeListChatModel(responses=["静默回答"])), skill_roots=[tmp_path])
    assert asyncio.run(conversation.arun("问题")) == "静默回答"
    output = capsys.readouterr()
    assert output.out == output.err == ""
