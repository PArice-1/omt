"""CLI 配置和终端隔离，不读取用户配置或占用真实输入终端。"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from omt.core import settings as agent_settings


@pytest.fixture(autouse=True)
def isolated_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_settings, "root", tmp_path / ".omt")
    agent_settings.load_settings.cache_clear()
    for key in ("DB_PATH", "WORKSPACE_PATH", "MCP_SERVERS", "SKILL_ROOTS", "MAX_CONCURRENT_RUNS"):
        monkeypatch.delenv(f"OMT_CLI_{key}", raising=False)
    monkeypatch.setenv("OMT_CLI_DB_PATH", str(tmp_path / "cli.sqlite"))
    monkeypatch.setenv("OMT_CLI_MCP_SERVERS", "{}")
    # 默认目录在 Settings 导入时已展开，改 HOME/root 不能隔离，必须显式覆盖。
    monkeypatch.setenv("OMT_CLI_SKILL_ROOTS", json.dumps([str(tmp_path / "skills")]))
    monkeypatch.setattr("omt.cli.app.PromptSession", lambda: None)
    yield
    agent_settings.load_settings.cache_clear()


@pytest.fixture
def cli_db(tmp_path: Path) -> Path:
    path = tmp_path / "cli.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        _ = connection.execute("""
            CREATE TABLE sessions (
                thread_id TEXT PRIMARY KEY NOT NULL, workspace_path TEXT NOT NULL, title TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        connection.commit()
    return path
