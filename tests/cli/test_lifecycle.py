"""CLI 完整组装、异常与信号回归，模型/MCP/文件均使用本地隔离数据。"""

import asyncio
import json
import sqlite3
import subprocess
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, closing
from importlib import import_module
from pathlib import Path
from textwrap import dedent
from typing import Self, override
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeListChatModel, FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.outputs import ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import Field

from omt.agent.agent import Agent
from omt.cli.app import main as session, run_cli
from omt.cli.settings import CliSettings
from omt.core import settings as agent_settings, bootstrap
from omt.core.prompts import load_system_prompt
from omt.core.session_manager import SessionManager
from omt.core.settings import load_settings
from omt.core.store import SessionStore


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("defaultProvider", raising=False)
    monkeypatch.delenv("defaultModel", raising=False)
    load_settings.cache_clear()
    directory = agent_settings.root / "agent"
    directory.mkdir(parents=True)
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


@pytest.mark.parametrize("skills_enabled", [True, False])
def test_cli_runs_manager_and_read_tool_without_network(
    config_dir: Path,
    tmp_path: Path,
    model_factory: Mock,
    cli_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    skills_enabled: bool,
) -> None:
    if not skills_enabled:
        monkeypatch.setenv("OMT_CLI_SKILL_ROOTS", "[]")

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

    note = tmp_path / "note.md"
    _ = note.write_text("测试文件的完整内容", encoding="utf-8")
    skill = tmp_path / "skills" / "review" / "SKILL.md"
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
    module = import_module("omt.cli.app")
    monkeypatch.setattr(module, "PromptSession", Mock())
    monkeypatch.setattr(
        module, "read_input", AsyncMock(side_effect=["读取说明", "继续", "/exit"])
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
    if skills_enabled:
        assert "## 可用 Skills" in system_text
        assert str(skill) in system_text
    else:
        assert "## 可用 Skills" not in system_text
        assert str(skill) not in system_text
    assert "SKILL_BODY_MARKER" not in system_text
    for messages in model.seen_messages:
        assert isinstance(messages[0], SystemMessage)
        assert messages[0].text == system_text
        assert sum(isinstance(message, SystemMessage) for message in messages) == 1

    async def verify_persistence() -> None:
        records = await SessionStore(cli_db).list_sessions(tmp_path)
        assert len(records) == 1
        assert records[0].title == "读取说明"
        config: RunnableConfig = {"configurable": {"thread_id": records[0].thread_id}}
        async with AsyncSqliteSaver.from_conn_string(str(cli_db)) as checkpointer:
            agent = Agent(model, checkpointer=checkpointer)
            snapshot = await agent.agent.aget_state(config)
        assert [message.type for message in snapshot.values["messages"]] == [
            "system", "human", "ai", "tool", "ai", "human", "ai",
        ]
        assert snapshot.values["messages"][-1].text == "继续完成。"

    asyncio.run(verify_persistence())


@pytest.mark.parametrize("error", [FileNotFoundError("资源缺失"), ValueError("提示词为空")])
def test_cli_stops_before_model_when_prompt_loading_fails(
    config_dir: Path, cli_db: Path, model_factory: Mock, monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    monkeypatch.setattr(bootstrap, "load_system_prompt", Mock(side_effect=error))
    with pytest.raises(type(error)) as raised:
        session()
    assert raised.value is error
    model_factory.assert_not_called()


@pytest.mark.parametrize("missing_table", [False, True])
def test_cli_invalid_database_fails_before_model_and_does_not_create_schema(
    config_dir: Path, cli_db: Path, model_factory: Mock, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, missing_table: bool,
) -> None:
    if missing_table:
        with closing(sqlite3.connect(cli_db)) as connection:
            _ = connection.execute("DROP TABLE sessions")
            connection.commit()
        expected = sqlite3.OperationalError
    else:
        path = tmp_path / "missing" / "state.sqlite"
        monkeypatch.setenv("OMT_CLI_DB_PATH", str(path))
        expected = FileNotFoundError
    with pytest.raises(expected):
        session()
    model_factory.assert_not_called()
    assert not (tmp_path / "missing").exists()
    with closing(sqlite3.connect(cli_db)) as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE name = 'sessions'").fetchall()
    assert bool(tables) is not missing_table


class ToolModel(FakeMessagesListChatModel):
    @override
    def bind_tools(self, tools: object, **kwargs: object) -> Self:
        return self


@pytest.mark.parametrize("cancel", [False, True])
def test_cli_failure_blocks_reexecution_and_external_cancel_cleans_up(
    cli_db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], cancel: bool,
) -> None:
    async def execute() -> None:
        started, cleaned = asyncio.Event(), asyncio.Event()
        invocations: list[str] = []

        @tool
        async def operation() -> str:
            """等待测试取消或模拟工具失败，不产生外部副作用。"""
            invocations.append("operation")
            started.set()
            try:
                if cancel:
                    await asyncio.Event().wait()
                raise RuntimeError("本地执行失败")
            finally:
                cleaned.set()

        model = ToolModel(responses=[AIMessage(content="", tool_calls=[{
            "name": "operation", "args": {}, "id": "operation-1",
        }])])

        @asynccontextmanager
        async def factory(options: CliSettings) -> AsyncGenerator[SessionManager]:
            async with AsyncSqliteSaver.from_conn_string(str(cli_db)) as saver:
                manager = SessionManager(
                    Agent(model, tools=[operation], checkpointer=saver), SessionStore(cli_db),
                    workspace_path=options.workspace_path, system_prompt="本地规则", skill_roots=[],
                )
                try:
                    yield manager
                finally:
                    await manager.aclose()
        reader = AsyncMock(side_effect=["执行", "不要重跑", "/exit"])
        monkeypatch.setattr("omt.cli.app.read_input", reader)
        before = asyncio.all_tasks()
        task = asyncio.create_task(run_cli(manager_factory=factory))
        try:
            await asyncio.wait_for(started.wait(), 5)
            if cancel:
                _ = task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
                assert reader.await_count == 1
            else:
                await asyncio.wait_for(task, 5)
                assert reader.await_count == 3
            assert cleaned.is_set() and invocations == ["operation"]
            assert not (asyncio.all_tasks() - before)
        finally:
            _ = task.cancel()
            _ = await asyncio.gather(task, return_exceptions=True)
    asyncio.run(execute())
    if cancel:
        assert "会话结束。" not in capsys.readouterr().out
    else:
        assert "上次运行未完成" in capsys.readouterr().out


def test_cli_sigint_exits_and_closes_manager(cli_db: Path, tmp_path: Path) -> None:
    # 信号只发送给测试创建的子进程；子进程不读取真实配置或发现 MCP。
    script = dedent('''\
        import asyncio
        import signal
        import sqlite3
        from collections.abc import AsyncGenerator
        from contextlib import asynccontextmanager, closing
        from pathlib import Path
        from typing import Self
        from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
        from langchain_core.messages import AIMessage
        from langchain_core.tools import tool
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        from omt.agent.agent import Agent
        import omt.cli.app as app
        from omt.cli.settings import CliSettings
        from omt.core.session_manager import SessionManager
        from omt.core.store import SessionStore

        class LocalModel(FakeMessagesListChatModel):
            def bind_tools(self, tools: object, **kwargs: object) -> Self:
                return self

        @tool
        async def wait_here() -> str:
            """等待本进程发出的 SIGINT。"""
            try:
                asyncio.get_running_loop().call_soon(signal.raise_signal, signal.SIGINT)
                await asyncio.Event().wait()
                return "不可到达"
            finally:
                print("TOOL_CLEANED", flush=True)

        @asynccontextmanager
        async def factory(options: CliSettings) -> AsyncGenerator[SessionManager]:
            async with AsyncSqliteSaver.from_conn_string(str(options.db_path)) as saver:
                model = LocalModel(responses=[AIMessage(content="", tool_calls=[{
                    "name": "wait_here", "args": {}, "id": "wait-1",
                }])])
                manager = SessionManager(
                    Agent(model, tools=[wait_here], checkpointer=saver), SessionStore(options.db_path),
                    workspace_path=options.workspace_path, system_prompt="规则", skill_roots=[],
                )
                try:
                    yield manager
                finally:
                    await manager.aclose()
                    print("MANAGER_CLOSED", flush=True)

        count = 0
        async def read_input(terminal: object) -> str:
            global count
            count += 1
            assert count == 1, "Ctrl-C 后不能继续读取输入"
            return "等待"

        app.open_manager = factory
        app.read_input = read_input
        app.PromptSession = lambda: None
        app.main()
        with closing(sqlite3.connect(CliSettings().db_path)) as connection:
            rows = connection.execute("SELECT workspace_path FROM sessions").fetchall()
        assert rows == [(str(Path.cwd()),)]
        print("INDEX_KEPT", flush=True)
    ''')
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(marker in result.stdout for marker in ("TOOL_CLEANED", "MANAGER_CLOSED", "INDEX_KEPT", "会话结束。"))
    assert "Traceback" not in result.stderr
