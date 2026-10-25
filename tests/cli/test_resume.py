"""/resume 交互测试：真实临时 SQLite、检查点和 Runtime，不访问模型或 MCP 服务。"""

import asyncio
import sqlite3
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, closing
from importlib import import_module
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from omt.agent.agent import Agent
from omt.cli.app import main
from omt.cli.settings import CliSettings
from omt.core.session_manager import SessionManager
from omt.core.store import SessionRecord, SessionStore


def session() -> None:
    """本组验证逐行模式的编号交互；TUI 选择面板由 test_tui.py 独立验证。"""
    main(["--plain"])


@pytest.fixture
def resume_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "resume.sqlite"
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

    @asynccontextmanager
    async def factory(options: CliSettings) -> AsyncGenerator[SessionManager]:
        async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
            manager = SessionManager(
                Agent(FakeListChatModel(responses=["新回答"]), checkpointer=saver),
                SessionStore(path), workspace_path=options.workspace_path,
                system_prompt="本地规则", skill_roots=[],
            )
            try:
                yield manager
            finally:
                await manager.aclose()

    module = import_module("omt.cli.app")
    monkeypatch.setenv("OMT_CLI_DB_PATH", str(path))
    monkeypatch.setattr(module, "open_manager", factory)
    monkeypatch.setattr(module, "PromptSession", Mock())
    return path


async def _seed_session(
    path: Path,
    workspace: Path,
    thread_id: str,
    title: str,
    created_at: str = "2025-01-01 00:00:00",
    updated_at: str = "2025-01-01 00:00:00",
    *,
    with_checkpoint: bool = True,
) -> None:
    await SessionStore(path).insert_session(thread_id, workspace, title)
    async with aiosqlite.connect(path) as connection:
        async with connection.execute(
            "UPDATE sessions SET created_at = ?, updated_at = ? WHERE thread_id = ?",
            (created_at, updated_at, thread_id),
        ):
            await connection.commit()
    if with_checkpoint:
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        async with AsyncSqliteSaver.from_conn_string(str(path)) as checkpointer:
            agent = Agent(FakeListChatModel(responses=["不应调用"]), checkpointer=checkpointer)
            _ = await agent.agent.aupdate_state(config, {"messages": [
                HumanMessage(content=f"历史问题：{title}"),
                AIMessage(content=f"历史回答：{title}"),
            ]}, as_node="model")


def _inputs(monkeypatch: pytest.MonkeyPatch, values: list[str]) -> None:
    module = import_module("omt.cli.app")
    monkeypatch.setattr(module, "read_input", AsyncMock(side_effect=values))


def _rows(output: str) -> list[list[str]]:
    return [line.split(" | ") for line in output.splitlines()
            if line.split(" | ")[0].isdigit()]


async def _message_texts(path: Path, thread_id: str) -> list[str]:
    async with AsyncSqliteSaver.from_conn_string(str(path)) as checkpointer:
        agent = Agent(FakeListChatModel(responses=["不应调用"]), checkpointer=checkpointer)
        snapshot = await agent.agent.aget_state({"configurable": {"thread_id": thread_id}})
        messages: list[AnyMessage] = snapshot.values.get("messages", [])
        return [message.text for message in messages]


def test_resume_lists_current_zero_then_update_order_for_current_directory(
    resume_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def prepare() -> None:
        await _seed_session(resume_db, tmp_path, "old", "旧对话", updated_at="2025-03-01 00:00:00")
        await _seed_session(resume_db, tmp_path, "new", "最新\n标题", created_at="2025-02-01")
        await _seed_session(resume_db, tmp_path / "other", "other", "其他目录", created_at="2030-01-01")

    asyncio.run(prepare())
    _inputs(monkeypatch, ["/resume", "/exit"])

    session()

    output = capsys.readouterr().out
    rows = _rows(output)
    assert "number | title | updated_at" in output
    assert "number | title | created_at" not in output
    assert [row[0] for row in rows] == ["0", "1", "2"]
    assert "当前" in rows[0][1]
    assert rows[0][2]
    assert rows[1][1:] == ["旧对话", "2025-03-01 00:00:00"]
    assert rows[2][1:] == ["最新 标题", "2025-01-01 00:00:00"]
    assert "其他目录" not in output
    assert "新回答" not in output
    assert len(asyncio.run(SessionStore(resume_db).list_sessions(tmp_path))) == 2


def test_resume_current_record_is_zero_and_not_duplicated(
    resume_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(_seed_session(resume_db, tmp_path, "old", "旧对话"))
    _inputs(monkeypatch, ["当前问题", "/resume", "/exit"])

    session()

    rows = _rows(capsys.readouterr().out)
    assert len(rows) == 2
    assert rows[0][0] == "0" and "当前问题" in rows[0][1] and "当前" in rows[0][1]
    assert rows[1][1] == "旧对话"
    records = asyncio.run(SessionStore(resume_db).list_sessions(tmp_path))
    current = next(record for record in records if record.thread_id != "old")
    assert asyncio.run(_message_texts(resume_db, current.thread_id))[-2:] == ["当前问题", "新回答"]


@pytest.mark.parametrize(("list_command", "select_command"), [
    ("  /ReSuMe  ", "  /RESUME 1  "),
    ("/resume", "resume 1"),
    ("resume", "/resume 1"),
    ("  ReSuMe  ", "  rEsUmE 1  "),
])
def test_resume_switches_before_first_prompt_and_continues_existing_checkpoint(
    resume_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], list_command: str, select_command: str,
) -> None:
    asyncio.run(_seed_session(resume_db, tmp_path, "target", "目标对话"))
    _inputs(monkeypatch, [list_command, select_command, "继续", "/resume", "/exit"])

    session()

    output = capsys.readouterr().out
    assert "[对话历史]" in output
    assert "历史问题：目标对话" in output
    assert "历史回答：目标对话" in output
    assert asyncio.run(_message_texts(resume_db, "target")) == [
        "历史问题：目标对话", "历史回答：目标对话", "继续", "新回答",
    ]
    records = asyncio.run(SessionStore(resume_db).list_sessions(tmp_path))
    assert [record.thread_id for record in records] == ["target"]
    assert records[0].title == "目标对话"
    assert records[0].created_at == "2025-01-01 00:00:00"
    assert records[0].updated_at != "2025-01-01 00:00:00"
    assert _rows(output)[-1][0] == "0" and "目标对话" in _rows(output)[-1][1]
    assert _rows(output)[-1][2] == records[0].updated_at


def test_resume_can_switch_back_to_original_conversation_after_refreshing_list(
    resume_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_seed_session(resume_db, tmp_path, "target", "目标对话"))
    _inputs(monkeypatch, [
        "当前问题", "/resume", "/resume 1", "/resume", "/resume 1", "回到当前", "/exit",
    ])

    session()

    records = asyncio.run(SessionStore(resume_db).list_sessions(tmp_path))
    assert len(records) == 2
    current = next(record for record in records if record.thread_id != "target")
    texts = asyncio.run(_message_texts(resume_db, current.thread_id))
    assert texts[-4:] == ["当前问题", "新回答", "回到当前", "新回答"]
    assert all("目标对话" not in text for text in texts)
    assert len(asyncio.run(_message_texts(resume_db, "target"))) == 2


@pytest.mark.parametrize("command", [
    "/resume 0", "/resume -1", "/resume 99", "/resume abc", "/resume 1 extra",
    "/resume 1.5", "/resume ²",
    "resume 0", "resume -1", "resume 99", "resume abc", "resume 1 extra",
    "resume 1.5", "resume ²",
])
def test_resume_invalid_or_current_numbers_do_not_execute_or_register(
    resume_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(_seed_session(resume_db, tmp_path, "target", "目标对话"))
    _inputs(monkeypatch, ["/resume", command, "/exit"])

    session()

    output = capsys.readouterr().out
    assert "[对话历史]" not in output
    assert "新回答" not in output
    assert any(text in output for text in ("当前会话", "编号无效", "用法"))
    records = asyncio.run(SessionStore(resume_db).list_sessions(tmp_path))
    assert len(records) == 1
    assert records[0].updated_at == "2025-01-01 00:00:00"


@pytest.mark.parametrize("select_command", ["/resume 1", "resume 1"])
def test_resume_requires_a_displayed_list_before_selecting_a_number(
    resume_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], select_command: str,
) -> None:
    asyncio.run(_seed_session(resume_db, tmp_path, "target", "目标对话"))
    _inputs(monkeypatch, [select_command, "/exit"])

    session()

    output = capsys.readouterr().out
    assert "编号无效" in output
    assert "[对话历史]" not in output
    assert len(asyncio.run(SessionStore(resume_db).list_sessions(tmp_path))) == 1


@pytest.mark.parametrize("change", ["delete", "move"])
def test_resume_revalidates_selected_record_before_switching(
    resume_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(_seed_session(resume_db, tmp_path, "target", "目标对话"))
    values = iter(["/resume", "/resume 1", "/exit"])

    async def read_input(_terminal: object) -> str:
        value = next(values)
        if value == "/resume 1":
            async with aiosqlite.connect(resume_db) as connection:
                if change == "delete":
                    async with connection.execute("DELETE FROM sessions WHERE thread_id = ?", ("target",)):
                        await connection.commit()
                else:
                    async with connection.execute(
                        "UPDATE sessions SET workspace_path = ? WHERE thread_id = ?",
                        (str(tmp_path / "other"), "target"),
                    ):
                        await connection.commit()
        return value

    monkeypatch.setattr(import_module("omt.cli.app"), "read_input", read_input)

    session()

    output = capsys.readouterr().out
    assert "不存在或不属于当前目录" in output
    assert "[对话历史]" not in output
    assert asyncio.run(SessionStore(resume_db).list_sessions(tmp_path)) == []


def test_resume_failed_switch_keeps_current_id_and_registration_state(
    resume_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def incomplete() -> None:
        await _seed_session(resume_db, tmp_path, "missing-state", "未完成历史", with_checkpoint=False)
        async with AsyncSqliteSaver.from_conn_string(str(resume_db)) as saver:
            agent = Agent(FakeListChatModel(responses=["不应调用"]), checkpointer=saver)
            _ = await agent.agent.aupdate_state(
                {"configurable": {"thread_id": "missing-state"}},
                {"messages": [HumanMessage(content="尚未完成")]},
            )
    asyncio.run(incomplete())
    _inputs(monkeypatch, ["/resume", "/resume 1", "新问题", "/exit"])

    session()

    output = capsys.readouterr().out
    assert "[会话恢复失败]" in output
    assert "[对话历史]" not in output
    records = asyncio.run(SessionStore(resume_db).list_sessions(tmp_path))
    assert len(records) == 2
    failed_target = next(record for record in records if record.thread_id == "missing-state")
    assert failed_target.updated_at == "2025-01-01 00:00:00"
    current = next(record for record in records if record.thread_id != "missing-state")
    assert current.title == "新问题"
    assert asyncio.run(_message_texts(resume_db, current.thread_id))[-2:] == ["新问题", "新回答"]


@pytest.mark.parametrize("method", ["list_sessions", "get_session"])
def test_resume_database_errors_are_reported_without_execution(
    resume_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(_seed_session(resume_db, tmp_path, "target", "目标对话"))
    monkeypatch.setattr(SessionStore, method, AsyncMock(side_effect=sqlite3.OperationalError("测试查询失败")))
    _inputs(monkeypatch, ["/resume", "/resume 1", "/exit"])

    session()

    output = capsys.readouterr().out
    assert "失败" in output
    assert "测试查询失败" not in output
    assert "[对话历史]" not in output
    assert "新回答" not in output
    with closing(sqlite3.connect(resume_db)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone() == (1,)


@pytest.mark.parametrize("list_command", ["/resume", "resume"])
def test_resume_empty_directory_displays_only_current_without_creating_index(
    resume_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], list_command: str,
) -> None:
    _inputs(monkeypatch, [list_command, "/resume 0", "/exit"])

    session()

    rows = _rows(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0][0] == "0" and "新对话" in rows[0][1] and "当前" in rows[0][1]
    assert asyncio.run(SessionStore(resume_db).list_sessions(tmp_path)) == []


def test_resume_timestamp_update_failure_keeps_restored_session_usable(
    resume_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(_seed_session(resume_db, tmp_path, "target", "目标对话"))
    monkeypatch.setattr(
        SessionStore, "update_session_updated_at",
        AsyncMock(side_effect=sqlite3.OperationalError("测试更新时间失败")),
    )
    _inputs(monkeypatch, ["/resume", "/resume 1", "继续", "/resume", "/exit"])

    session()

    output = capsys.readouterr().out
    assert "[修改时间更新失败]" in output
    assert "测试更新时间失败" not in output
    assert "会话已恢复" in output
    assert "[会话恢复失败]" not in output
    records = asyncio.run(SessionStore(resume_db).list_sessions(tmp_path))
    assert [record.thread_id for record in records] == ["target"]
    assert records[0].updated_at == "2025-01-01 00:00:00"
    assert _rows(output)[-1][0] == "0" and "目标对话" in _rows(output)[-1][1]
    assert asyncio.run(_message_texts(resume_db, "target")) == [
        "历史问题：目标对话", "历史回答：目标对话", "继续", "新回答",
    ]


def test_resume_uses_displayed_number_mapping_and_invalidates_it_after_switch(
    resume_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(_seed_session(resume_db, tmp_path, "target", "原编号一"))
    values = iter(["/resume", "/resume 1", "/resume 1", "继续", "/exit"])
    inserted = False

    async def read_input(_terminal: object) -> str:
        nonlocal inserted
        value = next(values)
        if value == "/resume 1" and not inserted:
            await _seed_session(resume_db, tmp_path, "newer", "新增记录", created_at="2030-01-01")
            inserted = True
        return value

    monkeypatch.setattr(import_module("omt.cli.app"), "read_input", read_input)

    session()

    output = capsys.readouterr().out
    assert "编号无效" in output
    assert asyncio.run(_message_texts(resume_db, "target")) == [
        "历史问题：原编号一", "历史回答：原编号一", "继续", "新回答",
    ]
    assert len(asyncio.run(_message_texts(resume_db, "newer"))) == 2
    assert len(asyncio.run(SessionStore(resume_db).list_sessions(tmp_path))) == 2


@pytest.mark.parametrize("user_input", [
    "  普通输入\n保留换行  ", "/resume-help", "resume-help", "/unknown", "/exit extra",
])
def test_input_dispatch_preserves_ordinary_text_and_unknown_commands(
    resume_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user_input: str,
) -> None:
    _inputs(monkeypatch, ["/resume", user_input, "/exit"])

    session()

    records = asyncio.run(SessionStore(resume_db).list_sessions(tmp_path))
    assert len(records) == 1
    assert records[0].title == user_input[:10]
    assert asyncio.run(_message_texts(resume_db, records[0].thread_id))[-2:] == [user_input, "新回答"]


def test_input_dispatch_clears_previous_numbers_when_list_refresh_fails(
    resume_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(_seed_session(resume_db, tmp_path, "target", "目标对话"))
    original_list = SessionStore.list_sessions
    calls = 0

    async def list_sessions(store: SessionStore, workspace_path: Path) -> list[SessionRecord]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("测试刷新失败")
        return await original_list(store, workspace_path)

    monkeypatch.setattr(SessionStore, "list_sessions", list_sessions)
    _inputs(monkeypatch, ["/resume", "/resume", "/resume 1", "/exit"])

    session()

    output = capsys.readouterr().out
    assert "[会话查询失败]" in output
    assert "测试刷新失败" not in output
    assert "编号无效" in output
    assert "[对话历史]" not in output
    assert len(asyncio.run(SessionStore(resume_db).list_sessions(tmp_path))) == 1
