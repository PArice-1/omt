"""会话切换测试：真实检查点、本地假模型和临时 SQLite，不访问真实服务。"""

import asyncio
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from omt.agent.agent import Agent
from omt.coding_agent.conversation import Conversation
from omt.cli.terminal import display_history


def test_load_history_loads_latest_sqlite_history_and_continues_without_duplicates(
    tmp_path: Path,
) -> None:
    async def execute() -> None:
        path = tmp_path / "checkpoints.sqlite"
        target_config: RunnableConfig = {"configurable": {"thread_id": "target"}}
        async with AsyncSqliteSaver.from_conn_string(str(path)) as checkpointer:
            source = Conversation(
                Agent(FakeListChatModel(responses=["旧回答一", "旧回答二"]),
                      checkpointer=checkpointer),
                target_config,
                system_prompt="旧系统规则",
                skill_roots=[],
            )
            _ = await source.arun("旧问题一")
            first = await source.agent.agent.aget_state(source.config)
            _ = await source.arun("旧问题二")
            expected_messages = list(source.messages)

        # 关闭再打开数据库，避免只验证同一实例的内存历史。
        async with AsyncSqliteSaver.from_conn_string(str(path)) as checkpointer:
            config: RunnableConfig = {
                "configurable": {
                    "thread_id": "current",
                    "checkpoint_id": first.config.get("configurable", {})["checkpoint_id"],
                    "checkpoint_ns": "old:subgraph",
                    "checkpoint_map": {"": "old-checkpoint"},
                    "custom_option": "keep",
                },
                "tags": ["local-test"],
                "metadata": {"source": "switch-test"},
                "recursion_limit": 20,
            }
            original_config = deepcopy(config)
            agent = Agent(FakeListChatModel(responses=["继续回答"]), checkpointer=checkpointer)
            conversation = Conversation(
                agent, config, system_prompt="新实例的规则不应混入旧会话", skill_roots=[]
            )

            previous_config = conversation.config
            await conversation.load_history("target")

            assert previous_config == original_config
            assert conversation.config is not previous_config
            assert conversation.messages == expected_messages
            assert conversation.messages[0].text == "旧系统规则"
            assert conversation.config.get("configurable", {}) == {
                "thread_id": "target", "custom_option": "keep",
            }
            assert conversation.config.get("tags") == ["local-test"]
            assert conversation.config.get("metadata") == {"source": "switch-test"}
            assert conversation.config.get("recursion_limit") == 20
            assert config == original_config
            before = await agent.agent.aget_state(target_config)
            assert conversation.messages == before.values["messages"]

            assert await conversation.arun("继续") == "继续回答"
            assert [message.text for message in conversation.messages] == [
                "旧系统规则", "旧问题一", "旧回答一", "旧问题二", "旧回答二", "继续", "继续回答",
            ]
            after = await agent.agent.aget_state(target_config)
            assert conversation.messages == after.values["messages"]
            assert after.config != before.config

    asyncio.run(execute())


def test_load_history_preserves_tool_metadata_and_can_switch_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def execute() -> None:
        model = FakeListChatModel(responses=["不应调用模型"])
        monkeypatch.setattr(
            FakeListChatModel, "_agenerate", AsyncMock(side_effect=AssertionError("不得执行模型"))
        )
        agent = Agent(model, checkpointer=InMemorySaver())
        current: RunnableConfig = {"configurable": {"thread_id": "current"}}
        target: RunnableConfig = {"configurable": {"thread_id": " target "}}
        current_messages = [HumanMessage(content="当前问题"), AIMessage(content="当前回答")]
        history = [
            SystemMessage(content="旧规则"),
            HumanMessage(content="读取说明"),
            AIMessage(content="", tool_calls=[{
                "name": "read_note", "args": {}, "id": "read-001",
            }]),
            ToolMessage(content="说明内容", tool_call_id="read-001", artifact={"raw": [1, 2]}),
            AIMessage(content="旧回答", response_metadata={"finish_reason": "stop"},
                      usage_metadata={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}),
        ]
        _ = await agent.agent.aupdate_state(current, {"messages": current_messages}, as_node="model")
        _ = await agent.agent.aupdate_state(target, {"messages": history}, as_node="model")
        conversation = Conversation(agent, current, skill_roots=[])

        await conversation.load_history(" target ")

        assert conversation.config.get("configurable", {})["thread_id"] == " target "
        assert conversation.messages == history
        request = conversation.messages[2]
        assert isinstance(request, AIMessage)
        assert request.tool_calls[0]["id"] == "read-001"
        result = conversation.messages[3]
        assert isinstance(result, ToolMessage)
        assert result.artifact == {"raw": [1, 2]}
        assert conversation.messages[-1].response_metadata == {"finish_reason": "stop"}
        final = conversation.messages[-1]
        assert isinstance(final, AIMessage)
        assert final.usage_metadata == {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}
        snapshot = await agent.agent.aget_state(target)
        assert conversation.messages is not snapshot.values["messages"]

        await conversation.load_history("current")

        assert conversation.config.get("configurable", {})["thread_id"] == "current"
        assert conversation.messages == current_messages

    asyncio.run(execute())


@pytest.mark.parametrize("thread_id", ["", " \t\n"])
def test_load_history_rejects_blank_id_without_changing_current_session(thread_id: str) -> None:
    conversation = Conversation(
        Agent(FakeListChatModel(responses=["未使用"])), system_prompt="当前规则", skill_roots=[]
    )
    previous_config, previous_messages = conversation.config, conversation.messages

    with pytest.raises(ValueError, match="thread_id"):
        asyncio.run(conversation.load_history(thread_id))

    assert conversation.config is previous_config
    assert conversation.messages is previous_messages


@pytest.mark.parametrize("with_checkpointer", [False, True])
def test_load_history_missing_state_or_checkpointer_keeps_current_session(
    with_checkpointer: bool, capsys: pytest.CaptureFixture[str],
) -> None:
    async def execute() -> None:
        agent = Agent(
            FakeListChatModel(responses=["未使用"]),
            checkpointer=InMemorySaver() if with_checkpointer else None,
        )
        conversation = Conversation(agent, system_prompt="当前规则", skill_roots=[])
        previous_config, previous_messages = conversation.config, conversation.messages

        with pytest.raises(ValueError):
            await conversation.load_history("missing")

        assert conversation.config is previous_config
        assert conversation.messages is previous_messages
        if with_checkpointer:
            snapshot = await agent.agent.aget_state({"configurable": {"thread_id": "missing"}})
            assert snapshot.created_at is None

    asyncio.run(execute())
    assert capsys.readouterr().out == ""


def test_load_history_rejects_pending_execution_without_changing_current_session() -> None:
    async def execute() -> None:
        agent = Agent(
            FakeListChatModel(responses=["不应调用"]),
            checkpointer=InMemorySaver(),
            interrupt_before=["model"],
        )
        target: RunnableConfig = {"configurable": {"thread_id": "pending"}}
        _ = await agent.ainvoke({"messages": [HumanMessage(content="待执行问题")]}, target)
        conversation = Conversation(agent, system_prompt="当前规则", skill_roots=[])
        previous_config, previous_messages = conversation.config, conversation.messages

        with pytest.raises(RuntimeError, match="未完成"):
            await conversation.load_history("pending")

        assert conversation.config is previous_config
        assert conversation.messages is previous_messages

    asyncio.run(execute())


@pytest.mark.parametrize("final_message", [
    HumanMessage(content="没有回答"),
    ToolMessage(content="只有工具结果", tool_call_id="unfinished"),
    AIMessage(content="", tool_calls=[{"name": "read_note", "args": {}, "id": "unfinished"}]),
])
def test_load_history_rejects_incomplete_history(final_message: BaseMessage) -> None:
    async def execute() -> None:
        agent = Agent(FakeListChatModel(responses=["未使用"]), checkpointer=InMemorySaver())
        target: RunnableConfig = {"configurable": {"thread_id": "incomplete"}}
        _ = await agent.agent.aupdate_state(target, {"messages": [final_message]}, as_node="model")
        conversation = Conversation(agent, system_prompt="当前规则", skill_roots=[])
        previous_config, previous_messages = conversation.config, conversation.messages

        with pytest.raises(RuntimeError, match="未完成"):
            await conversation.load_history("incomplete")

        assert conversation.config is previous_config
        assert conversation.messages is previous_messages

    asyncio.run(execute())


@pytest.mark.parametrize("cancel_query", [False, True])
def test_load_history_query_error_or_cancellation_keeps_current_session(
    cancel_query: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def execute() -> None:
        agent = Agent(FakeListChatModel(responses=["未使用"]), checkpointer=InMemorySaver())
        conversation = Conversation(agent, system_prompt="当前规则", skill_roots=[])
        previous_config, previous_messages = conversation.config, conversation.messages
        error = asyncio.CancelledError() if cancel_query else OSError("本地查询失败")
        monkeypatch.setattr(agent.agent, "aget_state", AsyncMock(side_effect=error))

        with pytest.raises(type(error)) as raised:
            await conversation.load_history("target")

        assert raised.value is error
        assert conversation.config is previous_config
        assert conversation.messages is previous_messages

    asyncio.run(execute())
    assert capsys.readouterr().out == ""


def test_load_history_displays_updated_history_in_order_without_internal_metadata(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def execute() -> None:
        monkeypatch.setattr(
            FakeListChatModel, "_agenerate", AsyncMock(side_effect=AssertionError("不得执行模型"))
        )
        agent = Agent(FakeListChatModel(responses=["不应调用"]), checkpointer=InMemorySaver())
        config: RunnableConfig = {"configurable": {"thread_id": "history"}}
        history = [
            SystemMessage(content="SYSTEM_PROMPT_PRIVATE"),
            HumanMessage(content="检查项目\n保留换行"),
            AIMessage(content=[{"type": "text", "text": "我先读取说明。"}], tool_calls=[{
                "name": "read_note", "args": {"name": "说明.md"}, "id": "read-001",
            }]),
            ToolMessage(content="说明内容", name="read_note", tool_call_id="read-001",
                        artifact={"private": "ARTIFACT_PRIVATE"}),
            AIMessage(content="", tool_calls=[
                {"name": "missing_note", "args": {}, "id": "missing-001"},
                {"name": "extra_note", "args": {}, "id": "unnamed-001"},
            ]),
            ToolMessage(content="说明不存在", name="missing_note", tool_call_id="missing-001",
                        status="error"),
            ToolMessage(content="无名称的补充结果", tool_call_id="unnamed-001"),
            AIMessage(content="最终历史回答。", response_metadata={"private": "MODEL_META_PRIVATE"}),
        ]
        _ = await agent.agent.aupdate_state(config, {"messages": history}, as_node="model")
        expected = deepcopy((await agent.agent.aget_state(config)).values["messages"])
        conversation = Conversation(agent, system_prompt="CURRENT_PROMPT_PRIVATE", skill_roots=[])

        await conversation.load_history("history")
        assert capsys.readouterr().out == ""
        display_history(conversation.messages)

        assert conversation.messages == expected
        assert (await agent.agent.aget_state(config)).values["messages"] == expected

    asyncio.run(execute())

    output = capsys.readouterr().out
    snippets = [
        "[对话历史]",
        "[用户] 检查项目\n保留换行",
        "[助手] 我先读取说明。",
        "[工具调用] read_note",
        '参数：{"name": "说明.md"}',
        "[工具结果] read_note\n说明内容",
        "[工具调用] missing_note",
        "[工具调用] extra_note",
        "[工具失败] missing_note\n说明不存在",
        "[工具结果] unnamed-001\n无名称的补充结果",
        "[助手] 最终历史回答。",
    ]
    positions = [output.index(text) for text in snippets]
    assert positions == sorted(positions)
    assert all(output.count(text) == 1 for text in snippets)
    for private in ("SYSTEM_PROMPT_PRIVATE", "ARTIFACT_PRIVATE", "MODEL_META_PRIVATE",
                    "CURRENT_PROMPT_PRIVATE", "[工具开始]", "[工具完成]"):
        assert private not in output


@pytest.mark.parametrize("system_prompt", ["", "SYSTEM_PROMPT_PRIVATE"])
def test_display_history_handles_empty_or_only_system_messages(
    system_prompt: str, capsys: pytest.CaptureFixture[str],
) -> None:
    conversation = Conversation(
        Agent(FakeListChatModel(responses=["未使用"])),
        system_prompt=system_prompt,
        skill_roots=[],
    )
    previous_messages = deepcopy(conversation.messages)

    display_history(conversation.messages)

    output = capsys.readouterr().out
    assert output.count("[对话历史]") == 1
    assert "暂无对话历史。" in output
    assert "SYSTEM_PROMPT_PRIVATE" not in output
    assert conversation.messages == previous_messages
