"""HTTP 与会话管理集成测试：临时 SQLite、真实 Agent 图和本地假模型。"""

import asyncio
import json
import sqlite3
from collections.abc import AsyncGenerator, Iterator, Sequence
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Any, Self, override
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.outputs import ChatResult
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import Field, SecretStr, ValidationError

from omt.agent.agent import Agent
from omt.api.app import create_app
from omt.api.settings import ApiSettings
from omt.coding_agent.configuration import StdioServerSettings, HttpServerSettings
from omt.coding_agent import bootstrap, settings as settings_module
from omt.ai.model import Model
from omt.coding_agent.session_manager import SessionError, SessionManager
from omt.coding_agent.store import SessionStore


TOKEN = "test-only-http-token-not-a-model-key"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
SCHEMA = """
CREATE TABLE sessions (
    thread_id TEXT PRIMARY KEY NOT NULL,
    workspace_path TEXT NOT NULL,
    title TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class LocalModel(FakeMessagesListChatModel):
    seen_messages: list[list[BaseMessage]] = Field(default_factory=list)

    @override
    def bind_tools(self, tools: Sequence[object], **kwargs: Any) -> Self:
        return self

    @override
    async def _agenerate(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: Any,
    ) -> ChatResult:
        self.seen_messages.append(list(messages))
        return await super()._agenerate(messages, stop, run_manager, **kwargs)


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings_module, "root", tmp_path / ".omt")
    settings_module.load_settings.cache_clear()
    # 即使宿主机配置了这些环境变量，离线测试也只使用显式配置。
    for name in ("OMT_API_ACCESS_TOKEN", "OMT_API_DB_PATH", "OMT_API_WORKSPACE_PATH",
                 "OMT_API_MCP_SERVERS", "OMT_API_SKILL_ROOTS", "OMT_API_MAX_CONCURRENT_RUNS",
                 "defaultProvider", "defaultModel"):
        monkeypatch.delenv(name, raising=False)
    yield
    settings_module.load_settings.cache_clear()


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "api.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        _ = connection.executescript(SCHEMA)
        connection.commit()
    return path


@pytest.fixture
def options(database: Path, tmp_path: Path) -> ApiSettings:
    return ApiSettings(access_token=SecretStr(TOKEN), db_path=database, workspace_path=tmp_path, skill_roots=[])


def application(options: ApiSettings, model: LocalModel, tools: Sequence[BaseTool] = ()) -> FastAPI:
    @asynccontextmanager
    async def factory(config: ApiSettings) -> AsyncGenerator[SessionManager]:
        async with AsyncSqliteSaver.from_conn_string(str(config.db_path)) as saver:
            manager = SessionManager(
                Agent(model, tools=tools, checkpointer=saver), SessionStore(config.db_path),
                workspace_path=config.workspace_path, system_prompt="私有系统提示词",
                skill_roots=[], max_concurrent_runs=config.max_concurrent_runs,
            )
            try:
                yield manager
            finally:
                await manager.aclose()
    return create_app(options, manager_factory=factory)


def new_session(client: TestClient) -> str:
    response = client.post("/api/v1/sessions", json={"title": "测试会话"}, headers=HEADERS)
    assert response.status_code == 201
    return response.json()["session_id"]


def test_http_auth_validation_and_health(options: ApiSettings) -> None:
    app = application(options, LocalModel(responses=[AIMessage(content="未调用")]))
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic invalid"}):
            response = client.get("/api/v1/sessions", headers=headers)
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "unauthorized"
        assert client.get("/docs").status_code == 404
        for message in ("", " \n", "a\x00b", "x" * 32001, 42):
            response = client.post(
                "/api/v1/sessions/missing/runs", json={"message": message}, headers=HEADERS,
            )
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "invalid_request"
            assert "input" not in response.text
        response = client.post("/api/v1/sessions", json={"workspace_path": "/arbitrary"}, headers=HEADERS)
        assert response.status_code == 422
        assert client.get("/api/v1/sessions", headers=HEADERS).json() == []


def test_http_multiturn_restart_empty_session_and_history_projection(
    options: ApiSettings, database: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    @tool(response_format="content_and_artifact")
    def local_note() -> tuple[str, dict[str, str]]:
        """返回本地测试结果及不可展示的内部字段。"""
        return "公开工具结果", {"internal": "不可展示的 artifact"}

    model = LocalModel(responses=[
        AIMessage(content="读取中", tool_calls=[{"name": "local_note", "args": {}, "id": "call-1"}]),
        AIMessage(content="第一轮回答", response_metadata={"internal": "模型内部元数据"}),
    ])
    app = application(options, model, [local_note])
    with TestClient(app) as client:
        session_id = new_session(client)
        empty_id = new_session(client)
        assert client.get(f"/api/v1/sessions/{session_id}/messages", headers=HEADERS).json() == []
        with closing(sqlite3.connect(database)) as connection:
            _ = connection.execute("UPDATE sessions SET updated_at = '2000-01-01 00:00:00'")
            connection.commit()
        response = client.post(f"/api/v1/sessions/{session_id}/runs", json={"message": "  读取\n"}, headers=HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed" and data["answer"] == "第一轮回答"
        assert data["run_id"] and data["session_id"] == session_id
        history = client.get(f"/api/v1/sessions/{session_id}/messages", headers=HEADERS)
        assert [item["role"] for item in history.json()] == ["user", "assistant", "tool", "assistant"]
        assert history.json()[0]["text"] == "  读取\n"
        assert history.json()[1]["tool_calls"][0]["id"] == "call-1"
        assert history.json()[2]["tool_call_id"] == "call-1"
        assert "私有系统提示词" not in history.text
        assert "不可展示的 artifact" not in history.text and "模型内部元数据" not in history.text
        assert client.get("/api/v1/sessions", headers=HEADERS).json()[0]["session_id"] == session_id

    # 新应用、新 Manager、新 Saver 连接，不复用内存会话。
    second_model = LocalModel(responses=[AIMessage(content="第二轮回答"), AIMessage(content="空会话首轮")])
    with TestClient(application(options, second_model, [local_note])) as client:
        response = client.post(f"/api/v1/sessions/{session_id}/runs", json={"message": "继续"}, headers=HEADERS)
        assert response.status_code == 200 and response.json()["answer"] == "第二轮回答"
        history = client.get(f"/api/v1/sessions/{session_id}/messages", headers=HEADERS).json()
        assert len(history) == 6
        assert sum(isinstance(item, SystemMessage) for item in second_model.seen_messages[0]) == 1
        assert client.post(f"/api/v1/sessions/{empty_id}/runs", json={"message": "首轮"}, headers=HEADERS).status_code == 200

    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute("SELECT updated_at, title FROM sessions WHERE thread_id = ?", (session_id,)).fetchone()
    assert row[0] != "2000-01-01 00:00:00" and row[1] == "测试会话"
    assert "[助手]" not in capsys.readouterr().out


def test_http_missing_foreign_and_incomplete_sessions(options: ApiSettings, tmp_path: Path) -> None:
    async def seed() -> None:
        store = SessionStore(options.db_path)
        await store.insert_session("foreign", tmp_path / "other")
        await store.insert_session("pending", tmp_path)
        async with AsyncSqliteSaver.from_conn_string(str(options.db_path)) as saver:
            agent = Agent(LocalModel(responses=[AIMessage(content="未调用")]), checkpointer=saver)
            _ = await agent.agent.aupdate_state(
                {"configurable": {"thread_id": "pending"}},
                {"messages": [HumanMessage(content="尚未完成")]},
            )
    asyncio.run(seed())
    model = LocalModel(responses=[AIMessage(content="未调用")])
    with TestClient(application(options, model)) as client:
        assert len(client.get("/api/v1/sessions", headers=HEADERS).json()) == 1
        for session_id in ("missing", "foreign"):
            for suffix in ("messages", "runs"):
                url = f"/api/v1/sessions/{session_id}/{suffix}"
                response = (client.get(url, headers=HEADERS) if suffix == "messages"
                            else client.post(url, json={"message": "hi"}, headers=HEADERS))
                assert response.status_code == 404
        response = client.post("/api/v1/sessions/pending/runs", json={"message": "hi"}, headers=HEADERS)
        assert response.status_code == 409 and response.json()["error"]["code"] == "session_incomplete"
    assert model.seen_messages == []


def test_http_errors_are_sanitized_and_failed_run_is_not_retried(options: ApiSettings) -> None:
    @tool
    async def broken() -> str:
        """模拟带内部细节的工具失败。"""
        raise RuntimeError("private-test-marker-do-not-expose")

    model = LocalModel(responses=[AIMessage(content="", tool_calls=[{"name": "broken", "args": {}, "id": "broken-1"}])])
    with TestClient(application(options, model, [broken])) as client:
        session_id = new_session(client)
        url = f"/api/v1/sessions/{session_id}/runs"
        response = client.post(url, json={"message": "执行"}, headers=HEADERS)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "run_failed"
        assert response.json()["error"]["run_id"]
        assert "private-test-marker" not in response.text
        retry = client.post(url, json={"message": "再次执行"}, headers=HEADERS)
        assert retry.status_code == 409
        with closing(sqlite3.connect(options.db_path)) as connection:
            _ = connection.execute("DROP TABLE sessions")
            connection.commit()
        response = client.get("/api/v1/sessions", headers=HEADERS)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "storage_unavailable"
        assert "sessions" not in response.json()["error"]["message"]


def test_timestamp_failure_returns_completed_with_warning(
    options: ApiSettings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken_update(self: SessionStore, thread_id: str) -> str:
        raise sqlite3.OperationalError("private database location")
    monkeypatch.setattr(SessionStore, "update_session_updated_at", broken_update)
    with TestClient(application(options, LocalModel(responses=[AIMessage(content="已完成")]))) as client:
        session_id = new_session(client)
        response = client.post(f"/api/v1/sessions/{session_id}/runs", json={"message": "执行"}, headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["answer"] == "已完成"
        assert response.json()["warnings"] == ["session_timestamp_update_failed"]
        assert len(client.get(f"/api/v1/sessions/{session_id}/messages", headers=HEADERS).json()) == 2


@pytest.mark.parametrize("limit", [1, 2])
def test_manager_concurrency_cancellation_and_shutdown(options: ApiSettings, limit: int) -> None:
    async def execute() -> None:
        started = asyncio.Event()
        cleaned = asyncio.Event()

        @tool
        async def wait_here() -> str:
            """等待测试取消，不操作外部资源。"""
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
            return "不可到达"

        model = LocalModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "wait_here", "args": {}, "id": "wait-1"}]),
            AIMessage(content="另一会话完成"),
        ])
        async with AsyncSqliteSaver.from_conn_string(str(options.db_path)) as saver:
            manager = SessionManager(
                Agent(model, tools=[wait_here], checkpointer=saver), SessionStore(options.db_path),
                workspace_path=options.workspace_path, system_prompt="系统", skill_roots=[],
                max_concurrent_runs=limit,
            )
            first = await manager.create_session()
            second = await manager.create_session()
            task = asyncio.create_task(manager.run(first.thread_id, "等待"))
            try:
                await asyncio.wait_for(started.wait(), 5)
                with pytest.raises(SessionError) as raised:
                    await manager.run(first.thread_id, "重入")
                assert raised.value.code == "session_busy"
                if limit == 1:
                    with pytest.raises(SessionError) as raised:
                        await manager.run(second.thread_id, "另一个")
                    assert raised.value.code == "capacity_exceeded"
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    assert cleaned.is_set()
                    assert (await manager.run(second.thread_id, "另一个")).answer == "另一会话完成"
                else:
                    assert (await manager.run(second.thread_id, "另一个")).answer == "另一会话完成"
                await manager.aclose()
                assert cleaned.is_set() and task.done()
                history = await saver.aget_tuple({"configurable": {"thread_id": second.thread_id}})
                assert history is not None
                assert [m.text for m in history.checkpoint["channel_values"]["messages"] if isinstance(m, HumanMessage)] == ["另一个"]
                with pytest.raises(SessionError) as raised:
                    await manager.run(second.thread_id, "关闭后")
                assert raised.value.code == "manager_closed"
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await manager.aclose()
    asyncio.run(execute())


def test_settings_and_startup_fail_closed(
    options: ApiSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError):
        _ = ApiSettings(access_token=SecretStr(" "))
    with pytest.raises(ValidationError):
        _ = ApiSettings()
    monkeypatch.setenv("OMT_API_ACCESS_TOKEN", TOKEN)
    assert ApiSettings().access_token.get_secret_value() == TOKEN
    # 默认组装必须在读取真实配置前拒绝缺库、缺表和错误 cwd。
    for update in ({"db_path": tmp_path / "missing.sqlite"}, {"workspace_path": tmp_path / "other"}):
        invalid = options.model_copy(update=update)
        with pytest.raises((FileNotFoundError, ValueError)):
            with TestClient(create_app(invalid)):
                pytest.fail("不应启动")
    assert not (tmp_path / "missing.sqlite").exists()
    with closing(sqlite3.connect(options.db_path)) as connection:
        _ = connection.execute("DROP TABLE sessions")
        connection.commit()
    with pytest.raises(sqlite3.OperationalError):
        with TestClient(create_app(options)):
            pytest.fail("缺少表时不应读取真实模型配置")


def test_default_lifespan_builds_dependencies_once_and_cleans_up(
    options: ApiSettings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = settings_module.root / "agent"
    directory.mkdir(parents=True)
    _ = (directory / "settings.json").write_text(json.dumps({
        "defaultProvider": "deepseek", "defaultModel": "test-model",
    }), encoding="utf-8")
    # 每轮使用独立消息，避免假模型循环复用已分配 ID 的对象替换旧历史。
    model = LocalModel(responses=[AIMessage(content="本地回答"), AIMessage(content="本地回答")])
    wrapper = Mock(spec=Model)
    wrapper.model = model
    model_factory = Mock(return_value=wrapper)
    discover = AsyncMock(return_value=[])
    monkeypatch.setattr(bootstrap, "build_model", model_factory)
    monkeypatch.setattr(bootstrap, "get_tools", discover)
    configured = options.model_copy(update={"mcp_servers": {
        "local": StdioServerSettings(transport="stdio", command="test-not-executed", args=[]),
    }})
    app = create_app(configured)
    model_factory.assert_not_called()
    discover.assert_not_called()
    with TestClient(app) as client:
        session_id = new_session(client)
        for message in ("第一轮", "第二轮"):
            response = client.post(f"/api/v1/sessions/{session_id}/runs", json={"message": message}, headers=HEADERS)
            assert response.status_code == 200
            assert response.json()["answer"] == "本地回答"
        model_factory.assert_called_once()
        discover.assert_awaited_once()
        assert discover.call_args.args[0]["local"]["command"] == "test-not-executed"
    # 离开 lifespan 后 Manager 不再可用，且 SQLite 连接已退出上下文。
    response = client.get("/api/v1/sessions", headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "manager_closed"


def test_mcp_configuration_is_explicit_and_secrets_are_not_rendered(
    options: ApiSettings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert options.mcp_servers == {}
    monkeypatch.setenv("OMT_API_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("OMT_API_MCP_SERVERS", json.dumps({
        "http": {"transport": "streamable_http", "url": "https://example.test/mcp",
                 "headers": {"Authorization": "test-only-private-header"}},
    }))
    configured = ApiSettings()
    connection = configured.mcp_servers["http"].connection()
    assert connection["transport"] == "streamable_http"
    assert isinstance(configured.mcp_servers["http"], HttpServerSettings)
    assert "test-only-private-header" not in repr(configured)
    assert TOKEN not in repr(configured)
    with pytest.raises(ValidationError) as raised:
        _ = ApiSettings(access_token=SecretStr("short-private-token"))
    assert "short-private-token" not in str(raised.value)


def test_cli_and_http_share_one_manager_without_leaking_terminal_output(
    options: ApiSettings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from httpx import ASGITransport, AsyncClient
    from prompt_toolkit import PromptSession
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.output import DummyOutput
    from omt.cli.app import run_cli
    from omt.cli.settings import CliSettings

    async def execute() -> None:
        async with AsyncSqliteSaver.from_conn_string(str(options.db_path)) as saver:
            model = LocalModel(responses=[AIMessage(content="CLI 回答"), AIMessage(content="HTTP 回答")])
            manager = SessionManager(
                Agent(model, checkpointer=saver), SessionStore(options.db_path),
                workspace_path=options.workspace_path, system_prompt="共享规则", skill_roots=[],
            )

            @asynccontextmanager
            async def http_factory(config: ApiSettings) -> AsyncGenerator[SessionManager]:
                # 此测试由最外层拥有资源；两个入口只借用同一实例。
                yield manager

            @asynccontextmanager
            async def cli_factory(config: CliSettings) -> AsyncGenerator[SessionManager]:
                yield manager

            app = create_app(options, manager_factory=http_factory)
            try:
                async with app.router.lifespan_context(app):
                    async with AsyncClient(transport=ASGITransport(app), base_url="http://local.test") as client:
                        response = await client.post("/api/v1/sessions", json={}, headers=HEADERS)
                        session_id = response.json()["session_id"]
                        reader = AsyncMock(side_effect=["/resume", "/resume 1", "CLI 问题", "/exit"])
                        monkeypatch.setattr("omt.cli.app.read_input", reader)
                        await run_cli(
                            CliSettings(db_path=options.db_path, workspace_path=options.workspace_path),
                            manager_factory=cli_factory,
                            terminal=PromptSession(input=DummyInput(), output=DummyOutput()),
                        )
                        assert capsys.readouterr().out.count("CLI 回答") == 1
                        response = await client.post(
                            f"/api/v1/sessions/{session_id}/runs", json={"message": "HTTP 问题"}, headers=HEADERS,
                        )
                        assert response.status_code == 200 and response.json()["answer"] == "HTTP 回答"
                        assert capsys.readouterr().out == ""
                        history = await client.get(f"/api/v1/sessions/{session_id}/messages", headers=HEADERS)
                        assert [m["text"] for m in history.json()] == ["CLI 问题", "CLI 回答", "HTTP 问题", "HTTP 回答"]
            finally:
                await manager.aclose()
    asyncio.run(execute())
