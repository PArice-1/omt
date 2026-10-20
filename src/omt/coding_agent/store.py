"""会话索引存储：读写预先创建的 sessions 表，不管理 LangGraph 检查点。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite


@dataclass(frozen=True)
class SessionRecord:
    """会话列表需要的元数据；完整消息和图状态由 Checkpointer 保存。"""

    thread_id: str
    workspace_path: str
    title: str | None
    created_at: str
    updated_at: str


def _session_from_row(row: aiosqlite.Row) -> SessionRecord:
    return SessionRecord(
        thread_id=str(row["thread_id"]),
        workspace_path=str(row["workspace_path"]),
        title=None if row["title"] is None else str(row["title"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


class SessionStore:
    """按 UUID 和工作目录管理已有数据库中的会话索引。

    实例化不访问数据库。每个异步操作自行连接、提交并关闭连接，不需要调用 close。
    数据库文件和 sessions 表必须事先创建；路径错误或 SQL 错误原样向调用方传播。
    不生成 thread_id，不读默认配置，也不修改 Checkpointer 管理的表。
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path: Path = db_path.expanduser().resolve()
        # 使用 URI 的 rw 模式，禁止路径错误时静默创建空数据库。
        self._database_uri: str = self.db_path.as_uri() + "?mode=rw"

    def _connect(self) -> aiosqlite.Connection:
        return aiosqlite.connect(self._database_uri, uri=True)

    async def insert_session(
        self,
        thread_id: str,
        workspace_path: Path,
        title: str | None = None,
    ) -> None:
        """插入会话并提交；重复 thread_id 抛 IntegrityError，不覆盖旧记录。"""
        if not thread_id.strip():
            raise ValueError("thread_id 不能为空")
        workspace = str(workspace_path.expanduser().resolve())
        async with self._connect() as connection:
            async with connection.execute(
                """
                INSERT INTO sessions (thread_id, workspace_path, title)
                VALUES (?, ?, ?)
                """,
                (thread_id, workspace, title),
            ):
                await connection.commit()

    async def update_session_updated_at(self, thread_id: str) -> str:
        """仅刷新指定会话的 UTC 修改时间，提交后返回新值；不存在时抛 ValueError。"""
        if not thread_id.strip():
            raise ValueError("thread_id 不能为空")
        updated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        async with self._connect() as connection:
            async with connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE thread_id = ?",
                (updated_at, thread_id),
            ) as cursor:
                if cursor.rowcount == 0:
                    raise ValueError("会话不存在，无法更新修改时间")
            await connection.commit()
        return updated_at

    async def get_session(self, thread_id: str) -> SessionRecord | None:
        """按完整 thread_id 查询元数据；不存在时返回 None。"""
        async with self._connect() as connection:
            connection.row_factory = aiosqlite.Row
            async with connection.execute(
                """
                SELECT thread_id, workspace_path, title, created_at, updated_at
                FROM sessions
                WHERE thread_id = ?
                """,
                (thread_id,),
            ) as cursor:
                row = await cursor.fetchone()
        return None if row is None else _session_from_row(row)

    async def list_sessions(self, workspace_path: Path) -> list[SessionRecord]:
        """列出精确匹配目录的会话，最近更新优先，同时间按创建时间和 ID 排序。"""
        workspace = str(workspace_path.expanduser().resolve())
        async with self._connect() as connection:
            connection.row_factory = aiosqlite.Row
            async with connection.execute(
                """
                SELECT thread_id, workspace_path, title, created_at, updated_at
                FROM sessions
                WHERE workspace_path = ?
                ORDER BY updated_at DESC, created_at DESC, thread_id ASC
                """,
                (workspace,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [_session_from_row(row) for row in rows]
