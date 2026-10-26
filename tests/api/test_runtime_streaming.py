"""运行设置、首次配置及 SSE 的离线契约；真实图和 SQLite，不访问上游。"""

import asyncio
import json
import os
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
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatResult
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.prebuilt import ToolRuntime
from starlette.types import Message, Scope
from starlette.requests import ClientDisconnect
from pydantic import SecretStr

from omt.agent.agent import Agent
from omt.ai.model import Model
from omt.api.app import create_app
from omt.api.schemas import StreamEvent
from omt.api.settings import ApiSettings
from omt.core import bootstrap, model_configuration, settings
from omt.core.session_manager import SessionError, SessionManager
from omt.core.store import SessionStore, initialize_session_database


MODELS = ["deepseek-flash", "deepseek-v4-pro", "test-plain"]
KEY = "test-only-preview-secret"


class RecordingModel(FakeMessagesListChatModel):
    seen: list[list[BaseMessage]] = []

    @override
    def bind_tools(self, tools: Sequence[object], **kwargs: Any) -> Self:
        return self

    @override
    async def _agenerate(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: Any,
    ) -> ChatResult:
        self.seen.append(list(messages))
        return await super()._agenerate(messages, stop, run_manager, **kwargs)


@pytest.fixture(autouse=True)
def isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "root", tmp_path / ".omt")
    for name in tuple(os.environ):
        if name.startswith("OMT_API_") or name in {"defaultProvider", "defaultModel"}:
            monkeypatch.delenv(name, raising=False)
    settings.load_settings.cache_clear()
    monkeypatch.setattr(model_configuration, "discover_models", AsyncMock(return_value=MODELS))
    monkeypatch.setattr(bootstrap, "get_tools", AsyncMock(return_value=[]))
    yield
    settings.load_settings.cache_clear()


@pytest.fixture
def config(tmp_path: Path) -> ApiSettings:
    return ApiSettings(db_path=tmp_path / "state.sqlite", workspace_path=tmp_path, skill_roots=[], mcp_servers={})


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> list[RecordingModel]:
    models: list[RecordingModel] = []

    def build(selection: settings.Settings, *, definition: settings.ModelDefinition | None = None) -> Model:
        model = RecordingModel(responses=[AIMessage(content=f"回答:{selection.defaultModel}"),
                                          AIMessage(content="后续回答")])
        models.append(model)
        wrapper = Mock(spec=Model)
        wrapper.model = model
        return wrapper

    monkeypatch.setattr(bootstrap, "build_model", build)
    return models


def seed(config: ApiSettings, model: str = "deepseek-flash") -> None:
    model_configuration.save_model_login(
        model_configuration.ModelLogin(provider="deepseek", api_key=SecretStr(KEY), model_name=model), model_names=MODELS,
    )
    initialize_session_database(config.db_path)


def preview(client: TestClient) -> str:
    response = client.post("/api/v1/model-logins", json={"provider": "deepseek", "api_key": KEY})
    assert response.status_code == 201
    assert KEY not in response.text
    return response.json()["login_id"]


def session(client: TestClient) -> str:
    response = client.post("/api/v1/sessions", json={})
    assert response.status_code == 201
    return response.json()["session_id"]


def events(body: str) -> list[StreamEvent]:
    return [StreamEvent.model_validate_json(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


@asynccontextmanager
async def manager_context(
    config: ApiSettings, model: RecordingModel, tools: Sequence[BaseTool] = (),
) -> AsyncGenerator[SessionManager]:
    initialize_session_database(config.db_path)
    async with AsyncSqliteSaver.from_conn_string(str(config.db_path)) as saver:
        manager = SessionManager(Agent(model, tools=tools, checkpointer=saver), SessionStore(config.db_path),
                                 workspace_path=config.workspace_path, system_prompt="私有系统提示词",
                                 skill_roots=[], max_concurrent_runs=config.max_concurrent_runs)
        try:
            yield manager
        finally:
            await manager.aclose()


def fixed_application(config: ApiSettings, model: RecordingModel, tools: Sequence[BaseTool] = ()) -> FastAPI:
    @asynccontextmanager
    async def factory(options: ApiSettings) -> AsyncGenerator[SessionManager]:
        async with manager_context(options, model, tools) as manager:
            yield manager
    return create_app(config, manager_factory=factory)


def test_first_configuration_is_preview_then_confirm_and_runs(config: ApiSettings, sdk: list[RecordingModel]) -> None:
    with TestClient(create_app(config)) as client:
        status = client.get("/api/v1/runtime").json()
        assert status["configured"] is False and status["ready"] is False
        assert client.get("/api/v1/models").json() == []
        assert client.get("/api/v1/providers").json() == [
            {"provider": "deepseek", "logged_in": False}, {"provider": "ollama", "logged_in": False},
        ]
        assert not settings.root.exists() and not config.db_path.exists() and not sdk
        assert client.post("/api/v1/sessions", json={}).json()["error"]["code"] == "runtime_not_ready"
        login_id = preview(client)
        assert not settings.root.exists() and not config.db_path.exists()
        invalid = client.post(f"/api/v1/model-logins/{login_id}/confirm", json={"model": "unknown"})
        assert invalid.status_code == 422
        response = client.post(f"/api/v1/model-logins/{login_id}/confirm", json={"model": "deepseek-flash"})
        assert response.status_code == 200
        assert response.json()["configuration_saved"] and response.json()["runtime_ready"]
        assert not response.json()["runtime"]["changing"]
        assert response.json()["error"] is None
        assert config.db_path.exists() and len(sdk) == 1
        expected_levels = ["none", "low", "high", "max"]
        assert response.json()["runtime"]["thinking"]["available_levels"] == expected_levels
        assert client.get("/api/v1/runtime").json()["thinking"]["available_levels"] == expected_levels
        catalog = client.get("/api/v1/models").json()
        assert set(item["model"] for item in catalog) == set(MODELS)
        assert next(item for item in catalog if item["model"] == "deepseek-flash")["available_levels"] == expected_levels
        assert client.post(f"/api/v1/model-logins/{login_id}/confirm", json={"model": "deepseek-flash"}).status_code == 404
        identifier = session(client)
        assert client.post(f"/api/v1/sessions/{identifier}/runs", json={"message": "你好"}).json()["answer"] == "回答:deepseek-flash"
        for path in ("/api/v1/runtime", "/api/v1/models", "/api/v1/providers"):
            assert KEY not in client.get(path).text


def test_preview_limits_expiry_and_errors_do_not_persist(
    config: ApiSettings, sdk: list[RecordingModel], monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr("omt.api.runtime.monotonic", lambda: now[0])
    limited = config.model_copy(update={"login_preview_limit": 1, "login_preview_ttl": 2})
    with TestClient(create_app(limited)) as client:
        login_id = preview(client)
        assert client.post("/api/v1/model-logins", json={"provider": "deepseek", "api_key": KEY}).status_code == 429
        now[0] += 3
        assert client.post(f"/api/v1/model-logins/{login_id}/confirm", json={"model": MODELS[0]}).status_code == 404
        preview(client)
        assert not settings.root.exists() and not sdk
    with TestClient(create_app(config)) as client:
        assert client.post(f"/api/v1/model-logins/{login_id}/confirm", json={"model": MODELS[0]}).status_code == 404
        monkeypatch.setattr(model_configuration, "discover_models", AsyncMock(side_effect=RuntimeError(KEY)))
        response = client.post("/api/v1/model-logins", json={"provider": "deepseek", "api_key": KEY})
        assert response.status_code == 422 and KEY not in response.text
        for body in ({"provider": "deepseek", "api_key": " "}, {"provider": "deepseek", "api_key": KEY, "base_url": "https://example.test"}):
            response = client.post("/api/v1/model-logins", json=body)
            assert response.status_code == 422 and KEY not in response.text
        assert not settings.root.exists() and not config.db_path.exists()


def test_confirmation_reports_saved_but_not_enabled(
    config: ApiSettings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap, "build_model", Mock(side_effect=RuntimeError(KEY)))
    with TestClient(create_app(config)) as client:
        login_id = preview(client)
        response = client.post(f"/api/v1/model-logins/{login_id}/confirm", json={"model": MODELS[0]})
        data = response.json()
        assert data["configuration_saved"] is True and data["runtime_ready"] is False
        assert data["error"]["code"] == "runtime_initialization_failed"
        assert KEY not in response.text
        assert settings.load_settings().defaultModel == MODELS[0]
        assert client.get("/api/v1/runtime").json()["ready"] is False


def test_quick_switch_and_thinking_preserve_history_and_configuration(
    config: ApiSettings, sdk: list[RecordingModel], monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(config)
    files = {path: path.read_bytes() for path in (settings.root / "agent").iterdir()}
    with TestClient(create_app(config)) as client:
        identifier = session(client)
        assert client.post(f"/api/v1/sessions/{identifier}/runs", json={"message": "第一轮"}).status_code == 200
        response = client.put("/api/v1/runtime/thinking", json={"level": "high"})
        assert response.status_code == 200 and response.json()["thinking"]["reasoning_effort"] == "high"
        assert response.json()["changing"] is False
        assert client.put("/api/v1/runtime/thinking", json={"level": "unknown"}).status_code == 422
        response = client.put("/api/v1/runtime/model", json={"provider": "deepseek", "model": "test-plain"})
        assert response.json()["thinking"]["level"] == "high"
        assert response.json()["thinking"]["reasoning_effort"] == "none"
        assert response.json()["thinking"]["available_levels"] == ["none"]
        assert client.post(f"/api/v1/sessions/{identifier}/runs", json={"message": "第二轮"}).json()["answer"] == "回答:test-plain"
        assert [m.text for m in sdk[-1].seen[0] if m.type == "human"] == ["第一轮", "第二轮"]
        assert len(client.get("/api/v1/sessions").json()) == 1
        assert files == {path: path.read_bytes() for path in (settings.root / "agent").iterdir()}
        monkeypatch.setattr(bootstrap, "build_model", Mock(side_effect=RuntimeError(KEY)))
        response = client.put("/api/v1/runtime/model", json={"provider": "deepseek", "model": MODELS[1]})
        assert response.status_code == 502 and KEY not in response.text
        assert client.get("/api/v1/runtime").json()["thinking"]["model"] == "test-plain"


def test_restart_uses_saved_defaults_not_temporary_selection(config: ApiSettings, sdk: list[RecordingModel]) -> None:
    seed(config)
    with TestClient(create_app(config)) as client:
        assert client.put("/api/v1/runtime/thinking", json={"level": "high"}).status_code == 200
        assert client.put("/api/v1/runtime/model", json={"provider": "deepseek", "model": "test-plain"}).status_code == 200
    with TestClient(create_app(config)) as client:
        state = client.get("/api/v1/runtime").json()["thinking"]
        assert state["model"] == "deepseek-flash" and state["level"] == "none"


def test_inflight_previews_are_limited_and_cancellation_releases_slot(
    config: ApiSettings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from httpx import ASGITransport, AsyncClient

    async def execute() -> None:
        entered = asyncio.Event()

        async def discover(connection: model_configuration.ProviderConnection) -> list[str]:
            entered.set()
            await asyncio.Event().wait()
            return MODELS

        monkeypatch.setattr(model_configuration, "discover_models", discover)
        app = create_app(config.model_copy(update={"login_preview_limit": 1}))
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app), base_url="http://local.test") as client:
                pending = asyncio.create_task(client.post("/api/v1/model-logins", json={"provider": "deepseek", "api_key": KEY}))
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    rejected = await client.post("/api/v1/model-logins", json={"provider": "deepseek", "api_key": KEY})
                    assert rejected.status_code == 429
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                    monkeypatch.setattr(model_configuration, "discover_models", AsyncMock(return_value=MODELS))
                    assert (await client.post("/api/v1/model-logins", json={"provider": "deepseek", "api_key": KEY})).status_code == 201
                    assert not settings.root.exists()
                finally:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
    asyncio.run(execute())


def test_switch_cancellation_blocks_new_work_and_keeps_old_runtime(
    config: ApiSettings, sdk: list[RecordingModel], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from httpx import ASGITransport, AsyncClient
    seed(config)

    async def execute() -> None:
        entered = asyncio.Event()
        app = create_app(config)
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app), base_url="http://local.test") as client:
                async def blocked_build(*args: object, **kwargs: object) -> Agent:
                    entered.set()
                    await asyncio.Event().wait()
                    raise AssertionError("不可到达")
                monkeypatch.setattr(bootstrap, "build_agent", blocked_build)
                pending = asyncio.create_task(client.put("/api/v1/runtime/model", json={"provider": "deepseek", "model": "test-plain"}))
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    assert (await client.get("/api/v1/runtime")).json()["changing"] is True
                    assert (await client.post("/api/v1/sessions", json={})).status_code == 409
                    assert (await client.put("/api/v1/runtime/thinking", json={"level": "high"})).status_code == 409
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                    current = (await client.get("/api/v1/runtime")).json()
                    assert current["changing"] is False and current["thinking"]["model"] == "deepseek-flash"
                    identifier = (await client.post("/api/v1/sessions", json={})).json()["session_id"]
                    response = await client.post(f"/api/v1/sessions/{identifier}/runs", json={"message": "继续"})
                    assert response.json()["answer"] == "回答:deepseek-flash"
                finally:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
    asyncio.run(execute())


def test_stream_protocol_reasoning_tool_links_and_restart_history(config: ApiSettings) -> None:
    @tool
    def note(value: str, runtime: ToolRuntime) -> str:
        """返回本地文本。"""
        runtime.emit_output_delta({"progress": value})
        return f"结果:{value}"

    model = RecordingModel(responses=[
        AIMessage(id="plan", content=[{"type": "reasoning", "reasoning": "先分析"},
                                       {"type": "text", "text": "读取中"}],
                  tool_calls=[{"id": "call-1", "name": "note", "args": {"value": "一"}},
                              {"id": "call-2", "name": "note", "args": {"value": "二"}}]),
        AIMessage(id="final", content=[{"type": "reasoning", "reasoning": "再总结"},
                                       {"type": "text", "text": "最终答案"}]),
    ])
    with TestClient(fixed_application(config, model, [note])) as client:
        identifier = session(client)
        response = client.post(f"/api/v1/sessions/{identifier}/runs/stream", json={"message": "执行"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        received = events(response.text)
        assert received[0].type == "run.started" and received[-1].type == "run.completed"
        assert [item.seq for item in received] == list(range(1, len(received) + 1))
        assert len({item.run_id for item in received}) == 1
        assert all(item.session_id == identifier for item in received)
        assert [item.data["delta"] for item in received if item.type == "assistant.reasoning.delta"] == ["先分析", "再总结"]
        assert received[-1].data["answer"] == "最终答案"
        tools = [item for item in received if item.type.startswith("tool.")]
        assert {item.tool_call_id for item in tools} == {"call-1", "call-2"}
        assert all(item.role == "tool" for item in tools)
        assert {item.tool_call_id for item in tools if item.type == "tool.delta"} == {"call-1", "call-2"}
        assistant_ids = {item.message_id for item in received if item.type == "assistant.started"}
        history = client.get(f"/api/v1/sessions/{identifier}/messages").json()
        assert assistant_ids == {item["message_id"] for item in history if item["role"] == "assistant"}
        assert [item.get("reasoning") for item in history if item["role"] == "assistant"] == ["先分析", "再总结"]
        assert "私有系统提示词" not in response.text
        schema = client.get("/openapi.json").json()
        stream_schema = schema["paths"]["/api/v1/sessions/{session_id}/runs/stream"]["post"]
        assert set(stream_schema["responses"]["200"]["content"]) == {"text/event-stream"}
        assert stream_schema["responses"]["422"]["content"]["application/json"]["schema"]["$ref"].endswith("/HttpErrorResponse")
    with TestClient(fixed_application(config, RecordingModel(responses=[AIMessage(content="后续")]))) as client:
        restored = client.get(f"/api/v1/sessions/{identifier}/messages").json()
        assert restored == history


def test_history_projects_legacy_reasoning_without_duplicating_standard_blocks(config: ApiSettings) -> None:
    model = RecordingModel(responses=[
        AIMessage(content="旧回答", additional_kwargs={"reasoning_content": "旧思考"}),
        AIMessage(content=[{"type": "reasoning", "reasoning": "标准思考"}, {"type": "text", "text": "新回答"}],
                  additional_kwargs={"reasoning_content": "标准思考"}),
    ])
    with TestClient(fixed_application(config, model)) as client:
        identifier = session(client)
        for prompt in ("第一轮", "第二轮"):
            assert client.post(f"/api/v1/sessions/{identifier}/runs", json={"message": prompt}).status_code == 200
        history = client.get(f"/api/v1/sessions/{identifier}/messages").json()
        assert [item["reasoning"] for item in history if item["role"] == "assistant"] == ["旧思考", "标准思考"]
        assert [item["text"] for item in history if item["role"] == "assistant"] == ["旧回答", "新回答"]


def test_stream_rejections_and_sanitized_execution_failure(config: ApiSettings) -> None:
    @tool
    async def broken() -> str:
        """抛出私有异常。"""
        raise RuntimeError(KEY)

    model = RecordingModel(responses=[AIMessage(content="", tool_calls=[{"name": "broken", "id": "bad", "args": {}}])])
    with TestClient(fixed_application(config, model, [broken])) as client:
        assert client.post("/api/v1/sessions/missing/runs/stream", json={"message": "执行"}).status_code == 404
        assert not model.seen
        identifier = session(client)
        response = client.post(f"/api/v1/sessions/{identifier}/runs/stream", json={"message": "执行"})
        received = events(response.text)
        assert response.status_code == 200 and received[-1].type == "run.failed"
        assert received[-1].data["code"] == "run_failed" and KEY not in response.text
        assert not any(item.type == "run.completed" for item in received)
        retry = client.post(f"/api/v1/sessions/{identifier}/runs/stream", json={"message": "重试"})
        assert retry.status_code == 409 and "run.started" not in retry.text


def test_handled_tool_error_and_timestamp_warning_still_complete(
    config: ApiSettings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    @tool
    def handled() -> ToolMessage:
        """返回已处理的工具错误，而非抛出执行异常。"""
        return ToolMessage(content="可展示的错误说明", status="error", tool_call_id="handled-call")

    async def fail_timestamp(self: SessionStore, thread_id: str) -> str:
        raise sqlite3.OperationalError(KEY)

    monkeypatch.setattr(SessionStore, "update_session_updated_at", fail_timestamp)
    model = RecordingModel(responses=[
        AIMessage(content="", tool_calls=[{"id": "handled-call", "name": "handled", "args": {}}]),
        AIMessage(content="错误已解释"),
    ])
    with TestClient(fixed_application(config, model, [handled])) as client:
        identifier = session(client)
        response = client.post(f"/api/v1/sessions/{identifier}/runs/stream", json={"message": "执行"})
        received = events(response.text)
        assert any(item.type == "tool.failed" and item.data["text"] == "可展示的错误说明" for item in received)
        assert received[-1].type == "run.completed"
        assert received[-1].data["warnings"] == ["session_timestamp_update_failed"]
        assert received[-1].data["answer"] == "错误已解释" and KEY not in response.text


def test_stream_storage_failure_before_admission_is_http_error(config: ApiSettings) -> None:
    model = RecordingModel(responses=[AIMessage(content="不应执行")])
    with TestClient(fixed_application(config, model)) as client:
        with closing(sqlite3.connect(config.db_path)) as connection:
            _ = connection.execute("DROP TABLE sessions")
            connection.commit()
        response = client.post("/api/v1/sessions/any/runs/stream", json={"message": "执行"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "storage_unavailable"
        assert "run.started" not in response.text and not model.seen


def request_scope(path: str) -> Scope:
    return {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "POST", "scheme": "http", "path": path,
            "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 1234), "server": ("local.test", 80)}


@pytest.mark.parametrize("mode", ["disconnect", "receive_disconnect", "send_timeout", "complete"])
def test_live_stream_is_incremental_and_disconnect_cleans_up(
    config: ApiSettings, sdk: list[RecordingModel], monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    seed(config)
    configured = config.model_copy(update={"stream_send_timeout": 0.5})

    async def execute() -> None:
        started, cleaned, release, got_data, allow_finish = (asyncio.Event() for _ in range(5))

        @tool
        async def wait_here() -> str:
            """等待测试释放或取消。"""
            started.set()
            try:
                await release.wait()
                return "完成"
            finally:
                cleaned.set()

        model = RecordingModel(responses=[AIMessage(content="准备", tool_calls=[{"name": "wait_here", "id": "wait", "args": {}}]),
                                          AIMessage(content="完成回答"), AIMessage(content="另一会话回答")])
        monkeypatch.setattr(bootstrap, "build_agent", AsyncMock(side_effect=lambda model, **kw: Agent(model.model, tools=[wait_here], checkpointer=kw["checkpointer"])))
        monkeypatch.setattr(bootstrap, "build_model", lambda *a, **kw: Mock(model=model))
        app = create_app(configured)
        from httpx import ASGITransport, AsyncClient
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app), base_url="http://local.test") as client:
                identifier = (await client.post("/api/v1/sessions", json={})).json()["session_id"]
                body = json.dumps({"message": "执行"}).encode()
                sent_request = False
                chunks: list[bytes] = []

                async def receive() -> Message:
                    nonlocal sent_request
                    if not sent_request:
                        sent_request = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    if mode == "receive_disconnect":
                        await got_data.wait()
                        await allow_finish.wait()
                        return {"type": "http.disconnect"}
                    await asyncio.Event().wait()
                    return {"type": "http.disconnect"}

                async def send(message: Message) -> None:
                    if message["type"] == "http.response.body" and message.get("body"):
                        chunks.append(message["body"])
                        if b"event: tool.started" in message["body"]:
                            got_data.set()
                            await started.wait()
                            await allow_finish.wait()
                            if mode == "disconnect":
                                raise OSError("client disconnected")
                            if mode in {"send_timeout", "receive_disconnect"}:
                                await asyncio.Event().wait()
                            release.set()

                scope = request_scope(f"/api/v1/sessions/{identifier}/runs/stream")
                if mode == "receive_disconnect":
                    scope["asgi"] = {"version": "3.0", "spec_version": "2.0"}
                task = asyncio.create_task(app(scope, receive, send))
                try:
                    await asyncio.wait_for(got_data.wait(), 5)
                    # 运行尚未结束时客户端已经拿到开始和正文，而非结束后一次返回。
                    assert any(b"event: run.started" in chunk for chunk in chunks)
                    assert any(b"event: assistant.text.delta" in chunk for chunk in chunks)
                    await asyncio.wait_for(started.wait(), 5)
                    assert (await client.get("/api/v1/runtime")).json()["busy"] is True
                    assert (await client.put("/api/v1/runtime/thinking", json={"level": "high"})).status_code == 409
                    assert (await client.post(f"/api/v1/sessions/{identifier}/runs/stream", json={"message": "重入"})).status_code == 409
                    other = (await client.post("/api/v1/sessions", json={})).json()["session_id"]
                    assert (await client.post(f"/api/v1/sessions/{other}/runs/stream", json={"message": "满载"})).status_code == 429
                    allow_finish.set()
                    if mode in {"complete", "receive_disconnect"}:
                        await asyncio.wait_for(task, 5)
                    else:
                        with pytest.raises(ClientDisconnect):
                            await asyncio.wait_for(task, 5)
                    await asyncio.wait_for(cleaned.wait(), 5)
                    assert (await client.get("/api/v1/runtime")).json()["busy"] is False
                    history = await client.get(f"/api/v1/sessions/{identifier}/messages")
                    assert history.status_code == (200 if mode == "complete" else 409)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
    asyncio.run(execute())


@pytest.mark.parametrize("limit", ["bytes", "events"])
def test_buffer_overflow_terminates_and_releases_capacity(config: ApiSettings, limit: str) -> None:
    update = {"stream_buffer_bytes": 1024} if limit == "bytes" else {"stream_buffer_events": 1}
    limited = config.model_copy(update=update)
    model = RecordingModel(responses=[AIMessage(content="X" * 4000), AIMessage(content="下一会话")])
    with TestClient(fixed_application(limited, model)) as client:
        identifier = session(client)
        response = client.post(f"/api/v1/sessions/{identifier}/runs/stream", json={"message": "大输出"})
        received = events(response.text)
        assert received[-1].type == "run.failed"
        assert received[-1].data["code"] == "stream_buffer_exceeded"
        assert "X" * 4000 not in response.text
        other = session(client)
        assert client.post(f"/api/v1/sessions/{other}/runs", json={"message": "继续"}).status_code == 200
