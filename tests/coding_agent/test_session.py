"""会话组装和终端输入测试：假模型、临时配置、内存终端，不调用真实提供商。"""

import asyncio
import json
import sqlite3
import subprocess
from contextlib import closing
import sys
from textwrap import dedent
from importlib import import_module
from pathlib import Path
from typing import Self, override
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeListChatModel, FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.outputs import ChatResult
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from prompt_toolkit import PromptSession
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.validation import ValidationError as InputValidationError
from pydantic import Field, SecretStr

from omt.agent.agent import Agent
from omt.coding_agent.prompts import load_system_prompt
from omt.coding_agent.session import (
    _aget_user_input,
    asession,
    _get_agent,
    _get_model,
    _get_user_input,
    _UserInputValidator,
    session,
)
from omt.coding_agent.settings import Settings, load_settings
from omt.coding_agent.store import SessionRecord, SessionStore


@pytest.fixture(autouse=True)
def session_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """会话测试只连接临时预建库，不启动真实 MCP Server。"""
    path = tmp_path / "cli.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        _ = connection.execute("""
            CREATE TABLE sessions (
                thread_id TEXT PRIMARY KEY NOT NULL,
                workspace_path TEXT NOT NULL,
                title TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        connection.commit()
    module = import_module("omt.coding_agent.session")
    monkeypatch.setattr(module, "db_path", path)
    monkeypatch.setattr(module, "get_tools", AsyncMock(return_value=[]))
    return path


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("defaultProvider", raising=False)
    monkeypatch.delenv("defaultModel", raising=False)
    load_settings.cache_clear()
    directory = tmp_path / "agent"
    directory.mkdir()
    (directory / "settings.json").write_text(json.dumps({
        "defaultProvider": "deepseek", "defaultModel": "deepseek-chat",
    }), encoding="utf-8")
    (directory / "auth.json").write_text(json.dumps({
        "deepseek": {"type": "api_key", "key": "test-only-key"},
    }), encoding="utf-8")
    (directory / "model-store.json").write_text(json.dumps({
        "deepseek": {"models": [{
            "id": "deepseek-chat", "provider": "deepseek", "baseUrl": "https://example.test/v1",
        }]},
    }), encoding="utf-8")
    yield directory
    load_settings.cache_clear()


@pytest.fixture
def model_factory(monkeypatch: pytest.MonkeyPatch) -> Mock:
    factory = Mock(return_value=FakeListChatModel(responses=["本地回复"]))
    monkeypatch.setattr(import_module("omt.ai.models.deepseek"), "init_chat_model", factory)
    return factory


def test_get_model_joins_settings_auth_and_matching_catalog_entry(config_dir, model_factory):
    client = _get_model(load_settings())

    assert client.provider == "deepseek"
    assert client.model is model_factory.return_value
    model_factory.assert_called_once_with(
        model_provider="deepseek",
        model="deepseek-chat",
        base_url="https://example.test/v1",
        api_key=SecretStr("test-only-key"),
    )


@pytest.mark.parametrize("auth", [
    {},
    {"deepseek": {"type": "api_key"}},
    {"deepseek": {"type": "api_key", "key": ""}},
    {"deepseek": {"type": "api_key", "key": " \n\t"}},
])
def test_get_model_rejects_missing_or_blank_auth(config_dir, model_factory, auth):
    (config_dir / "auth.json").write_text(json.dumps(auth), encoding="utf-8")

    with pytest.raises(ValueError, match="认证|API Key"):
        _get_model(load_settings())

    model_factory.assert_not_called()


@pytest.mark.parametrize("auth_type", ["oauth", "bearer", "unknown"])
def test_get_model_explicitly_rejects_unsupported_auth_types(config_dir, model_factory, auth_type):
    (config_dir / "auth.json").write_text(json.dumps({
        "deepseek": {"type": auth_type, "access": "test-token-not-an-api-key"},
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="认证类型.*不支持") as raised:
        _get_model(load_settings())

    assert "test-token-not-an-api-key" not in str(raised.value)
    model_factory.assert_not_called()


@pytest.mark.parametrize("catalog", [
    {},
    {"deepseek": {"models": []}},
    {"deepseek": {"models": [{"id": "different-model", "baseUrl": "https://example.test"}]}},
    {"deepseek": {"models": [{
        "id": "deepseek-chat", "provider": "wrong-provider", "baseUrl": "https://example.test",
    }]}},
])
def test_get_model_rejects_missing_or_mismatched_catalog(config_dir, model_factory, catalog):
    (config_dir / "model-store.json").write_text(json.dumps(catalog), encoding="utf-8")

    with pytest.raises(ValueError, match="model-store.json|提供商"):
        _get_model(load_settings())

    model_factory.assert_not_called()


def test_get_model_uses_url_of_exact_selected_model_not_first_entry(config_dir, model_factory):
    (config_dir / "model-store.json").write_text(json.dumps({
        "deepseek": {"models": [
            {"id": "other", "baseUrl": "https://wrong.example.test"},
            {"id": "deepseek-chat", "baseUrl": "https://selected.example.test/v1"},
        ]},
    }), encoding="utf-8")

    _get_model(load_settings())

    assert model_factory.call_args.kwargs["base_url"] == "https://selected.example.test/v1"


def test_get_model_rejects_unregistered_provider_before_initializing_model(config_dir, model_factory):
    settings = Settings(defaultProvider="not-registered", defaultModel="some-model")

    with pytest.raises(ValueError, match="提供商.*未注册"):
        _get_model(settings)

    model_factory.assert_not_called()


def test_get_agent_uses_underlying_chat_model_and_builtin_tools(
    config_dir: Path, model_factory: Mock
) -> None:
    client = _get_model(load_settings())

    agent = asyncio.run(_get_agent(client))

    assert isinstance(agent, Agent)
    assert agent.model is client.model
    assert agent.tools is not None
    assert {getattr(item, "name", None) for item in agent.tools} == {"read", "edit", "write", "bash"}


@pytest.mark.parametrize("asynchronous", [False, True])
def test_get_user_input_uses_real_prompt_session_without_builtin_input(monkeypatch, asynchronous: bool):
    forbidden_input = Mock(side_effect=AssertionError("不得使用内置 input"))
    monkeypatch.setattr("builtins.input", forbidden_input)
    with create_pipe_input() as pipe:
        terminal = PromptSession[str](input=pipe, output=DummyOutput())
        pipe.send_text("  你好  \n")

        result = (asyncio.run(_aget_user_input(terminal)) if asynchronous
                  else _get_user_input(terminal))

    assert result == "  你好  "
    forbidden_input.assert_not_called()


@pytest.mark.parametrize("value", ["", "   ", "\n\t", "hello\x00world"])
def test_input_validator_rejects_blank_or_nul_input(value):
    with pytest.raises(InputValidationError):
        _UserInputValidator().validate(Document(value))


def test_input_validator_accepts_unicode_and_multiline():
    _UserInputValidator().validate(Document("分析这个项目\n并列出问题"))


@pytest.mark.parametrize("asynchronous", [False, True])
def test_prompt_rejects_blank_line_then_accepts_next_input(asynchronous: bool):
    with create_pipe_input() as pipe:
        terminal = PromptSession[str](input=pipe, output=DummyOutput())
        pipe.send_text("\n你好\n")

        result = (asyncio.run(_aget_user_input(terminal)) if asynchronous
                  else _get_user_input(terminal))
        assert result == "你好"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(("control", "exception"), [("\x03", KeyboardInterrupt), ("\x04", EOFError)])
def test_prompt_propagates_cancel_and_eof(control, exception, asynchronous: bool):
    with create_pipe_input() as pipe:
        terminal = PromptSession[str](input=pipe, output=DummyOutput())
        pipe.send_text(control)

        with pytest.raises(exception):
            if asynchronous:
                asyncio.run(_aget_user_input(terminal))
            else:
                _get_user_input(terminal)


@pytest.mark.parametrize("exit_command", ["/exit", "/quit", " /EXIT "])
def test_session_exits_without_sending_exit_command_to_model(
    config_dir: Path,
    model_factory: Mock,
    session_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_command: str,
) -> None:
    module = import_module("omt.coding_agent.session")
    runner = Mock()
    runner_factory = Mock(return_value=runner)
    monkeypatch.setattr(module, "CreateAgentSession", runner_factory)
    monkeypatch.setattr(module, "PromptSession", Mock())
    monkeypatch.setattr(module, "_aget_user_input", AsyncMock(return_value=exit_command))

    session()

    runner_factory.assert_called_once()
    runner.run.assert_not_called()
    runner.arun.assert_not_called()
    assert asyncio.run(SessionStore(session_db).list_sessions(config_dir.parent)) == []


def test_session_handles_eof_and_does_not_print_answer_twice(
    config_dir: Path,
    model_factory: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = import_module("omt.coding_agent.session")
    runner = Mock()
    runner.arun = AsyncMock(return_value="只由 runtime 输出的回答")
    monkeypatch.setattr(module, "CreateAgentSession", Mock(return_value=runner))
    terminal_factory = Mock()
    monkeypatch.setattr(module, "PromptSession", terminal_factory)
    read_input = AsyncMock(side_effect=["你好", EOFError()])
    monkeypatch.setattr(module, "_aget_user_input", read_input)

    session()

    runner.arun.assert_awaited_once_with("你好")
    runner.run.assert_not_called()
    terminal_factory.assert_called_once()
    assert all(call.args == (terminal_factory.return_value,) for call in read_input.call_args_list)
    assert "只由 runtime 输出的回答" not in capsys.readouterr().out


def test_session_runs_real_runtime_and_read_tool_without_network(
    config_dir: Path,
    model_factory: Mock,
    session_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class ToolCapableModel(FakeMessagesListChatModel):
        seen_messages: list[list[BaseMessage]] = Field(default_factory=list)

        @override
        def bind_tools(self, tools: object, **kwargs: object) -> Self:
            return self

        @override
        def _generate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: CallbackManagerForLLMRun | None = None,
            **kwargs: object,
        ) -> ChatResult:
            self.seen_messages.append(list(messages))
            return super()._generate(messages, stop, run_manager, **kwargs)

    note = config_dir.parent / "note.md"
    _ = note.write_text("测试文件的完整内容", encoding="utf-8")
    skill = config_dir.parent / "skills" / "review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    _ = skill.write_text(
        "---\nname: review\ndescription: 审查代码\n---\nSKILL_BODY_MARKER\n",
        encoding="utf-8",
    )
    model = ToolCapableModel(responses=[
        AIMessage(content="我先读取文件。", tool_calls=[{
            "name": "read", "args": {"file": str(note)}, "id": "local-read",
        }]),
        AIMessage(content="最终总结：这是一份测试说明。"),
        AIMessage(content="继续完成。"),
    ])
    model_factory.return_value = model
    module = import_module("omt.coding_agent.session")
    monkeypatch.setattr(module, "PromptSession", Mock())
    monkeypatch.setattr(
        module, "_aget_user_input", AsyncMock(side_effect=["读取说明", "继续", "/exit"])
    )

    session()

    output = capsys.readouterr().out
    assert "[工具开始] read" in output
    assert "测试文件的完整内容" in output
    assert "[工具完成] read" in output
    assert output.count("最终总结：这是一份测试说明。") == 1
    assert "test-only-key" not in output
    model_factory.assert_called_once()
    assert len(model.seen_messages) == 3
    system_text = model.seen_messages[0][0].text
    assert system_text.startswith(load_system_prompt())
    assert "## 可用 Skills" in system_text
    assert str(skill) in system_text
    assert "SKILL_BODY_MARKER" not in system_text
    for messages in model.seen_messages:
        assert isinstance(messages[0], SystemMessage)
        assert messages[0].text == system_text
        assert sum(isinstance(message, SystemMessage) for message in messages) == 1

    async def verify_persistence() -> None:
        records = await SessionStore(session_db).list_sessions(config_dir.parent)
        assert len(records) == 1
        assert records[0].title == "读取说明"
        config: RunnableConfig = {"configurable": {"thread_id": records[0].thread_id}}
        async with AsyncSqliteSaver.from_conn_string(str(session_db)) as checkpointer:
            agent = Agent(model, checkpointer=checkpointer)
            snapshot = await agent.agent.aget_state(config)
        assert [message.type for message in snapshot.values["messages"]] == [
            "system", "human", "ai", "tool", "ai", "human", "ai",
        ]
        assert snapshot.values["messages"][-1].text == "继续完成。"

    asyncio.run(verify_persistence())


@pytest.mark.usefixtures("config_dir")
@pytest.mark.parametrize("error", [FileNotFoundError("资源缺失"), ValueError("提示词为空")])
def test_session_stops_before_model_initialization_when_prompt_loading_fails(
    model_factory: Mock,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    module = import_module("omt.coding_agent.session")
    monkeypatch.setattr(module, "load_system_prompt", Mock(side_effect=error))
    runner_factory = Mock()
    monkeypatch.setattr(module, "CreateAgentSession", runner_factory)

    with pytest.raises(type(error)) as raised:
        session()

    assert raised.value is error
    model_factory.assert_not_called()
    runner_factory.assert_not_called()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_get_user_input_can_create_its_own_prompt_session(monkeypatch, asynchronous: bool):
    module = import_module("omt.coding_agent.session")
    with create_pipe_input() as pipe:
        terminal = PromptSession[str](input=pipe, output=DummyOutput())
        factory = Mock(return_value=terminal)
        monkeypatch.setattr(module, "PromptSession", factory)
        pipe.send_text("独立读取输入\n")

        result = (asyncio.run(_aget_user_input()) if asynchronous else _get_user_input())
        assert result == "独立读取输入"

    factory.assert_called_once_with()


def test_session_ctrl_c_during_input_exits_without_starting_a_turn(
    config_dir: Path,
    model_factory: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = import_module("omt.coding_agent.session")
    runner = Mock(arun=AsyncMock())
    read_input = AsyncMock(side_effect=[KeyboardInterrupt(), "/exit"])
    monkeypatch.setattr(module, "CreateAgentSession", Mock(return_value=runner))
    monkeypatch.setattr(module, "PromptSession", Mock())
    monkeypatch.setattr(module, "_aget_user_input", read_input)

    session()

    assert read_input.await_count == 1
    runner.arun.assert_not_awaited()
    output = capsys.readouterr().out
    assert "会话结束。" in output
    assert "已取消本次输入" not in output
    assert "Ctrl-C 取消" not in output


def test_session_allows_retry_after_failed_turn(config_dir, model_factory, monkeypatch):
    module = import_module("omt.coding_agent.session")
    runner = Mock()
    runner.arun = AsyncMock(side_effect=[RuntimeError("本地测试失败"), "第二次成功"])
    monkeypatch.setattr(module, "CreateAgentSession", Mock(return_value=runner))
    monkeypatch.setattr(module, "PromptSession", Mock())
    monkeypatch.setattr(module, "_aget_user_input", AsyncMock(side_effect=["第一轮", "重试", "/exit"]))

    session()

    assert [call.args for call in runner.arun.await_args_list] == [("第一轮",), ("重试",)]


def test_session_reuses_one_event_loop_for_input_and_turns(
    config_dir: Path, model_factory: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = import_module("omt.coding_agent.session")
    loops: list[asyncio.AbstractEventLoop] = []
    inputs = iter(["第一轮", "第二轮", "/exit"])

    async def read_input(terminal: object) -> str:
        loops.append(asyncio.get_running_loop())
        return next(inputs)

    async def run_turn(message: str) -> str:
        loops.append(asyncio.get_running_loop())
        return "仅由 runtime 展示"

    runner = Mock(arun=AsyncMock(side_effect=run_turn))
    monkeypatch.setattr(module, "CreateAgentSession", Mock(return_value=runner))
    monkeypatch.setattr(module, "PromptSession", Mock())
    monkeypatch.setattr(module, "_aget_user_input", read_input)

    session()

    assert len(loops) == 5
    assert all(loop is loops[0] for loop in loops)
    runner.run.assert_not_called()
    assert runner.arun.await_count == 2


def test_session_sigint_during_execution_exits_and_cleans_up(tmp_path: Path) -> None:
    # 真实 SIGINT 只发给本测试启动的子进程，不中断 pytest 或用户终端。
    script = dedent('''\
        import asyncio
        import signal
        import sqlite3
        from contextlib import closing
        from pathlib import Path
        from importlib import import_module
        from typing import Self
        from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
        from langchain_core.messages import AIMessage
        from langchain_core.runnables import RunnableConfig
        from langchain_core.tools import tool
        from langgraph.checkpoint.base import BaseCheckpointSaver
        from omt.agent.agent import Agent
        from omt.coding_agent.runtime import CreateAgentSession

        class LocalModel(FakeMessagesListChatModel):
            def bind_tools(self, tools: object, **kwargs: object) -> Self:
                return self

        @tool
        async def wait_note() -> str:
            """等待取消并清理本地异步工具。"""
            try:
                asyncio.get_running_loop().call_soon(signal.raise_signal, signal.SIGINT)
                await asyncio.Event().wait()
                return "不应完成"
            finally:
                print("TOOL_CLEANED", flush=True)

        model = LocalModel(responses=[AIMessage(content="", tool_calls=[
            {"name": "wait_note", "args": {}, "id": "wait"}
        ])])
        module = import_module("omt.coding_agent.session")
        module.db_path = Path.cwd() / "cli.sqlite"
        module.load_settings = lambda: None
        module.load_system_prompt = lambda: "本地规则"
        module._get_model = lambda settings: None
        conversations: list[CreateAgentSession] = []

        async def get_agent(
            client: object, *, checkpointer: BaseCheckpointSaver[str]
        ) -> Agent:
            return Agent(model, tools=[wait_note], checkpointer=checkpointer)

        def create_conversation(
            agent: Agent, config: RunnableConfig, **kwargs: object
        ) -> CreateAgentSession:
            conversation = CreateAgentSession(
                agent, config, system_prompt="本地规则", skill_roots=[]
            )
            conversations.append(conversation)
            return conversation

        module._get_agent = get_agent
        module.CreateAgentSession = create_conversation
        module.PromptSession = lambda: None
        read_count = 0

        async def read_input(terminal: object) -> str:
            global read_count
            read_count += 1
            assert read_count == 1, "Ctrl-C 后不得开始下一轮输入"
            return "等待说明"

        module._aget_user_input = read_input
        module.session()
        assert read_count == 1
        conversation = conversations[0]
        assert [message.type for message in conversation.messages] == ["system"], (
            "取消回合不得提交内存历史"
        )
        with closing(sqlite3.connect(module.db_path)) as connection:
            rows = connection.execute(
                "SELECT thread_id, workspace_path FROM sessions"
            ).fetchall()
        assert rows == [(
            conversation.config["configurable"]["thread_id"], str(Path.cwd().resolve())
        )], "取消前已保存同一 ID 的目录索引"
        print("INDEX_KEPT", flush=True)
        print("CLI_RETURNED", flush=True)
    ''')
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "TOOL_CLEANED" in result.stdout
    assert "INDEX_KEPT" in result.stdout
    assert "CLI_RETURNED" in result.stdout
    assert "会话结束。" in result.stdout
    assert "本轮已中断" not in result.stdout
    assert "Traceback" not in result.stderr


def test_asession_propagates_external_cancellation_and_cleans_up(
    config_dir: Path,
    model_factory: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = import_module("omt.coding_agent.session")
    read_input = AsyncMock(return_value="等待")
    monkeypatch.setattr(module, "PromptSession", Mock())
    monkeypatch.setattr(module, "_aget_user_input", read_input)

    async def execute() -> None:
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def run_turn(message: str) -> str:
            started.set()
            try:
                await asyncio.Event().wait()
                return "不应完成"
            finally:
                cleaned.set()

        runner = Mock(arun=AsyncMock(side_effect=run_turn))
        monkeypatch.setattr(module, "CreateAgentSession", Mock(return_value=runner))
        task = asyncio.create_task(asession())
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert cleaned.is_set()
        assert read_input.await_count == 1

    asyncio.run(execute())
    assert "会话结束。" not in capsys.readouterr().out


@pytest.mark.parametrize("first_turn_fails", [False, True])
def test_session_registers_once_before_running_with_the_same_thread_and_workspace(
    config_dir: Path,
    model_factory: Mock,
    session_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_turn_fails: bool,
) -> None:
    module = import_module("omt.coding_agent.session")
    store = SessionStore(session_db)
    records_at_turn: list[list[SessionRecord]] = []
    first_prompt = "这是首轮需要记录的完整问题"

    async def run_turn(message: str) -> str:
        records_at_turn.append(await store.list_sessions(Path.cwd()))
        if first_turn_fails and len(records_at_turn) == 1:
            raise RuntimeError("本地执行失败")
        return "完成"

    runner = Mock(arun=AsyncMock(side_effect=run_turn))
    factory = Mock(return_value=runner)
    monkeypatch.setattr(module, "CreateAgentSession", factory)
    monkeypatch.setattr(module, "PromptSession", Mock())
    monkeypatch.setattr(
        module, "_aget_user_input", AsyncMock(side_effect=[first_prompt, "下一轮", "/exit"])
    )

    session()

    records = asyncio.run(store.list_sessions(config_dir.parent))
    assert len(records) == 1
    record = records[0]
    assert UUID(record.thread_id).version == 4
    config = factory.call_args.kwargs["config"]
    assert record.thread_id == config["configurable"]["thread_id"]
    assert record.workspace_path == str(config_dir.parent.resolve())
    assert record.workspace_path != str(session_db)
    assert record.title == first_prompt[:10]
    assert records_at_turn == [[record], [record]]
    assert runner.arun.await_count == 2


def test_session_retries_registration_after_storage_failure_without_running_agent(
    config_dir: Path,
    model_factory: Mock,
    session_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = import_module("omt.coding_agent.session")
    original_insert = SessionStore.insert_session
    attempts: list[str] = []

    async def insert_session(
        self: SessionStore,
        thread_id: str,
        workspace_path: Path,
        title: str | None = None,
    ) -> None:
        attempts.append(thread_id)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("测试索引写入失败")
        await original_insert(self, thread_id, workspace_path, title)

    runner = Mock(arun=AsyncMock(return_value="完成"))
    monkeypatch.setattr(SessionStore, "insert_session", insert_session)
    monkeypatch.setattr(module, "CreateAgentSession", Mock(return_value=runner))
    monkeypatch.setattr(module, "PromptSession", Mock())
    monkeypatch.setattr(
        module, "_aget_user_input", AsyncMock(side_effect=["第一次", "重试写入", "/exit"])
    )

    session()

    runner.arun.assert_awaited_once_with("重试写入")
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    records = asyncio.run(SessionStore(session_db).list_sessions(config_dir.parent))
    assert len(records) == 1
    assert records[0].title == "重试写入"
    output = capsys.readouterr().out
    assert "[会话存储失败]" in output
    assert "本轮尚未执行" in output
    assert "本轮执行未完成" not in output


def test_session_missing_database_fails_without_creating_it_or_initializing_model(
    config_dir: Path,
    model_factory: Mock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = import_module("omt.coding_agent.session")
    missing = tmp_path / "not-created" / "omt.sqlite"
    monkeypatch.setattr(module, "db_path", missing)

    with pytest.raises(FileNotFoundError, match="数据库"):
        session()

    assert not missing.parent.exists()
    model_factory.assert_not_called()


def test_session_missing_table_is_reported_without_running_agent_or_creating_table(
    config_dir: Path,
    model_factory: Mock,
    session_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with closing(sqlite3.connect(session_db)) as connection:
        _ = connection.execute("DROP TABLE sessions")
        connection.commit()
    module = import_module("omt.coding_agent.session")
    runner = Mock(arun=AsyncMock())
    monkeypatch.setattr(module, "CreateAgentSession", Mock(return_value=runner))
    monkeypatch.setattr(module, "PromptSession", Mock())
    monkeypatch.setattr(module, "_aget_user_input", AsyncMock(side_effect=["问题", "/exit"]))

    session()

    runner.arun.assert_not_awaited()
    assert "[会话存储失败]" in capsys.readouterr().out
    with closing(sqlite3.connect(session_db)) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'sessions'"
        ).fetchall() == []
