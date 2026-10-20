"""会话索引测试：只操作临时 SQLite，建表由测试显式完成。"""

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from omt.coding_agent.store import SessionRecord, SessionStore


SCHEMA = """
CREATE TABLE sessions (
    thread_id TEXT PRIMARY KEY NOT NULL,
    workspace_path TEXT NOT NULL,
    title TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_sessions_workspace ON sessions(workspace_path, updated_at);
"""


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "会话 # index.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        _ = connection.executescript(SCHEMA)
        connection.commit()
    return path


def test_constructor_does_not_create_database_or_parent(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "omt.sqlite"

    store = SessionStore(path)

    assert store.db_path == path.resolve()
    assert not path.parent.exists()


@pytest.mark.parametrize("title", [None, "", "审查代码'; DROP TABLE sessions; --"])
def test_insert_session_is_committed_and_can_be_read_by_a_new_store(
    db_path: Path, tmp_path: Path, title: str | None
) -> None:
    workspace = tmp_path / "项目 ' workspace"
    asyncio.run(SessionStore(db_path).insert_session("uuid-001", workspace, title))

    record = asyncio.run(SessionStore(db_path).get_session("uuid-001"))

    assert isinstance(record, SessionRecord)
    assert record.thread_id == "uuid-001"
    assert record.workspace_path == str(workspace.resolve())
    assert record.title == title
    assert record.created_at
    assert record.updated_at == record.created_at
    assert asyncio.run(SessionStore(db_path).list_sessions(workspace)) == [record]


def test_insert_session_without_title_and_missing_queries(
    db_path: Path, tmp_path: Path
) -> None:
    async def execute() -> None:
        store = SessionStore(db_path)
        assert await store.get_session("missing") is None
        assert await store.list_sessions(tmp_path) == []
        await store.insert_session("uuid-001", tmp_path)
        record = await store.get_session("uuid-001")
        assert record is not None
        assert record.title is None

    asyncio.run(execute())


def test_duplicate_id_does_not_overwrite_and_store_remains_usable(
    db_path: Path, tmp_path: Path
) -> None:
    async def execute() -> None:
        store = SessionStore(db_path)
        await store.insert_session("uuid-001", tmp_path, "原始标题")
        with pytest.raises(sqlite3.IntegrityError):
            await store.insert_session("uuid-001", tmp_path / "other", "覆盖标题")
        await store.insert_session("uuid-002", tmp_path, "后续插入")
        original = await store.get_session("uuid-001")
        assert original is not None
        assert original.title == "原始标题"
        assert original.workspace_path == str(tmp_path.resolve())
        assert len(await store.list_sessions(tmp_path)) == 2

    asyncio.run(execute())


def test_list_sessions_filters_exact_workspace_and_orders_deterministically(
    db_path: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / "project"
    rows = [
        ("old", str(workspace), "旧会话", "2025-01-01", "2025-01-01"),
        ("b", str(workspace), "同时间 B", "2025-02-01", "2025-03-01"),
        ("a", str(workspace), "同时间 A", "2025-02-01", "2025-03-01"),
        ("created-earlier", str(workspace), None, "2025-01-01", "2025-03-01"),
        ("new", str(workspace), "新会话", "2025-04-01", "2025-04-01"),
        ("other", str(tmp_path / "other"), None, "2026-01-01", "2026-01-01"),
        ("child", str(workspace / "src"), None, "2026-01-01", "2026-01-01"),
    ]
    with closing(sqlite3.connect(db_path)) as connection:
        _ = connection.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)", rows)
        connection.commit()

    records = asyncio.run(SessionStore(db_path).list_sessions(workspace))

    assert [record.thread_id for record in records] == [
        "new", "a", "b", "created-earlier", "old",
    ]
    assert all(record.workspace_path == str(workspace) for record in records)


def test_paths_are_normalized_without_changing_thread_id(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    relative_db = Path(db_path.name)
    store = SessionStore(relative_db)
    asyncio.run(store.insert_session(" opaque-id ", Path("~/project/../project")))

    records = asyncio.run(store.list_sessions(Path("project")))

    assert store.db_path == db_path.resolve()
    assert len(records) == 1
    assert records[0].thread_id == " opaque-id "
    assert records[0].workspace_path == str(tmp_path / "project")


@pytest.mark.parametrize("thread_id", ["", " \t\n"])
def test_insert_rejects_blank_id_before_database_access(
    tmp_path: Path, thread_id: str
) -> None:
    path = tmp_path / "missing.sqlite"

    with pytest.raises(ValueError, match="thread_id"):
        asyncio.run(SessionStore(path).insert_session(thread_id, tmp_path))

    assert not path.exists()


@pytest.mark.parametrize("thread_id", ["target", " opaque-'id;-- "])
def test_update_session_updated_at_persists_and_only_changes_target_timestamp(
    db_path: Path, tmp_path: Path, thread_id: str,
) -> None:
    store = SessionStore(db_path)
    asyncio.run(store.insert_session(thread_id, tmp_path, "原始标题"))
    asyncio.run(store.insert_session("other", tmp_path, "其他会话"))
    with closing(sqlite3.connect(db_path)) as connection:
        _ = connection.execute(
            "UPDATE sessions SET created_at = '2000-01-01 00:00:00', updated_at = '2001-01-01 00:00:00'"
        )
        connection.commit()
    original = asyncio.run(store.get_session(thread_id))
    other = asyncio.run(store.get_session("other"))
    assert original is not None
    before = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")

    updated_at = asyncio.run(store.update_session_updated_at(thread_id))

    after = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    reopened = SessionStore(db_path)
    assert before <= updated_at <= after
    assert updated_at != original.updated_at
    assert asyncio.run(reopened.get_session(thread_id)) == replace(original, updated_at=updated_at)
    assert asyncio.run(reopened.get_session("other")) == other
    assert [record.thread_id for record in asyncio.run(reopened.list_sessions(tmp_path))] == [
        thread_id, "other",
    ]


def test_update_session_updated_at_rejects_missing_record_without_inserting(
    db_path: Path, tmp_path: Path,
) -> None:
    store = SessionStore(db_path)

    with pytest.raises(ValueError, match="不存在"):
        asyncio.run(store.update_session_updated_at("missing"))

    assert asyncio.run(store.list_sessions(tmp_path)) == []


@pytest.mark.parametrize("thread_id", ["", " \t\n"])
def test_update_session_updated_at_rejects_blank_id_before_database_access(
    tmp_path: Path, thread_id: str,
) -> None:
    path = tmp_path / "missing.sqlite"

    with pytest.raises(ValueError, match="thread_id"):
        asyncio.run(SessionStore(path).update_session_updated_at(thread_id))

    assert not path.exists()


async def _call_store(
    store: SessionStore,
    operation: Literal["insert", "get", "list", "update"],
    workspace: Path,
) -> None:
    if operation == "insert":
        await store.insert_session("uuid-001", workspace)
    elif operation == "get":
        _ = await store.get_session("uuid-001")
    elif operation == "update":
        _ = await store.update_session_updated_at("uuid-001")
    else:
        _ = await store.list_sessions(workspace)


@pytest.mark.parametrize("operation", ["insert", "get", "list", "update"])
def test_missing_database_is_not_created(
    tmp_path: Path, operation: Literal["insert", "get", "list", "update"]
) -> None:
    path = tmp_path / "missing.sqlite"

    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(_call_store(SessionStore(path), operation, tmp_path))

    assert not path.exists()


@pytest.mark.parametrize("operation", ["insert", "get", "list", "update"])
def test_missing_table_is_reported_without_initializing_schema(
    tmp_path: Path, operation: Literal["insert", "get", "list", "update"]
) -> None:
    path = tmp_path / "empty.sqlite"
    with closing(sqlite3.connect(path)):
        pass

    with pytest.raises(sqlite3.OperationalError, match="no such table: sessions"):
        asyncio.run(_call_store(SessionStore(path), operation, tmp_path))

    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
