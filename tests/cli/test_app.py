"""CLI 通过真实 Manager 管理会话，离线验证入口与 HTTP 共用的业务契约。"""

import asyncio
import sqlite3
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from importlib import import_module
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from omt.agent.agent import Agent
from omt.cli.app import main
from omt.cli.settings import CliSettings
from omt.core.session_manager import SessionManager
from omt.core.store import SessionStore


@pytest.fixture
def local_manager(cli_db: Path, monkeypatch: pytest.MonkeyPatch) -> FakeListChatModel:
    model = FakeListChatModel(responses=["第一轮回答", "第二轮回答"])

    @asynccontextmanager
    async def factory(options: CliSettings) -> AsyncGenerator[SessionManager]:
        async with AsyncSqliteSaver.from_conn_string(str(options.db_path)) as saver:
            manager = SessionManager(
                Agent(model, checkpointer=saver), SessionStore(options.db_path),
                workspace_path=options.workspace_path, system_prompt="本地系统提示词", skill_roots=[],
            )
            try:
                yield manager
            finally:
                await manager.aclose()
    monkeypatch.setattr(import_module("omt.cli.app"), "open_manager", factory)
    return model


def feed(monkeypatch: pytest.MonkeyPatch, values: list[str | BaseException]) -> AsyncMock:
    reader = AsyncMock(side_effect=values)
    monkeypatch.setattr(import_module("omt.cli.app"), "read_input", reader)
    return reader


@pytest.mark.parametrize("ending", ["/exit", " /QUIT ", EOFError(), KeyboardInterrupt()])
def test_cli_exit_does_not_create_empty_session(
    cli_db: Path, local_manager: FakeListChatModel, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, ending: str | BaseException,
) -> None:
    reader = feed(monkeypatch, [ending])
    main()
    assert reader.await_count == 1
    assert asyncio.run(SessionStore(cli_db).list_sessions(tmp_path)) == []


def test_cli_multiturn_uses_manager_without_duplicate_output(
    cli_db: Path, local_manager: FakeListChatModel, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _ = feed(monkeypatch, ["  第一条输入\n", "第二条", EOFError()])
    main()
    output = capsys.readouterr().out
    assert output.count("第一轮回答") == 1 and output.count("第二轮回答") == 1
    assert "本地系统提示词" not in output

    async def verify() -> None:
        records = await SessionStore(cli_db).list_sessions(tmp_path)
        assert len(records) == 1 and records[0].title == "  第一条输入\n"
        async with AsyncSqliteSaver.from_conn_string(str(cli_db)) as saver:
            agent = Agent(local_manager, checkpointer=saver)
            snapshot = await agent.agent.aget_state({"configurable": {"thread_id": records[0].thread_id}})
        assert [m.text for m in snapshot.values["messages"] if isinstance(m, HumanMessage)] == ["  第一条输入\n", "第二条"]
    asyncio.run(verify())


def test_cli_registration_failure_can_retry_without_executing_first_input(
    cli_db: Path, local_manager: FakeListChatModel, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    original = SessionStore.insert_session
    attempts: list[str] = []

    async def insert(self: SessionStore, thread_id: str, workspace_path: Path, title: str | None = None) -> None:
        attempts.append(thread_id)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("本地测试失败")
        await original(self, thread_id, workspace_path, title)
    monkeypatch.setattr(SessionStore, "insert_session", insert)
    _ = feed(monkeypatch, ["不要执行", "重新登记", "/exit"])
    main()
    records = asyncio.run(SessionStore(cli_db).list_sessions(tmp_path))
    assert len(records) == 1 and records[0].title == "重新登记"
    assert "本轮尚未执行" in capsys.readouterr().out


def test_cli_and_api_configuration_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMT_API_MAX_CONCURRENT_RUNS", "8")
    monkeypatch.setenv("OMT_CLI_MAX_CONCURRENT_RUNS", "2")
    options = CliSettings()
    assert options.max_concurrent_runs == 2
    assert options.mcp_servers == {}
    assert not hasattr(options, "access_token")


def test_cli_fixture_uses_only_temporary_skill_roots(tmp_path: Path) -> None:
    assert CliSettings().skill_roots == [tmp_path / "skills"]


@pytest.mark.parametrize("selection", ["default", "custom", "disabled"])
def test_cli_skill_roots_use_user_default_or_explicit_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str,
) -> None:
    """selection 分别验证默认用户目录、自定义相对目录和空列表关闭；不扫描目录。"""
    monkeypatch.setenv("OMT_API_SKILL_ROOTS", '["/unused-api-skills"]')
    if selection == "default":
        monkeypatch.delenv("OMT_CLI_SKILL_ROOTS")
        expected = [Path("~/.agents/skills").expanduser().resolve()]
    elif selection == "custom":
        monkeypatch.setenv("OMT_CLI_SKILL_ROOTS", '["./custom-skills"]')
        expected = [tmp_path / "custom-skills"]
    else:
        monkeypatch.setenv("OMT_CLI_SKILL_ROOTS", "[]")
        expected = []

    roots = CliSettings().skill_roots
    assert roots is not None
    assert [root.resolve() for root in roots] == expected


def test_cli_preserves_legacy_mcp_default_but_can_disable_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMT_CLI_MCP_SERVERS")
    options = CliSettings()
    assert options.mcp_servers["knowledge"].connection()["transport"] == "stdio"
    monkeypatch.setenv("OMT_CLI_MCP_SERVERS", "{}")
    assert CliSettings().mcp_servers == {}
