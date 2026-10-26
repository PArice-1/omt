"""推理强度的 CLI 与生命周期回归；只使用临时 SQLite、假凭据和本地模型。"""

import asyncio
import json
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from importlib import import_module
from pathlib import Path
from typing import Self, override
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import Field

from omt.agent.agent import Agent
from omt.ai.model import Model
from omt.cli.app import run_cli
from omt.cli.settings import CliSettings
from omt.core import bootstrap, settings as settings_module
from omt.core.configuration import StdioServerSettings
from omt.core.conversation import Conversation
from omt.core.session_manager import SessionError, SessionManager
from omt.core.store import SessionStore


class RecordingModel(FakeMessagesListChatModel):
    seen: list[list[BaseMessage]] = Field(default_factory=list)

    @override
    def bind_tools(self, tools: Sequence[object], **kwargs: object) -> Self:
        return self

    @override
    async def _agenerate(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: object,
    ) -> ChatResult:
        self.seen.append(list(messages))
        return await super()._agenerate(messages, stop, run_manager, **kwargs)


@pytest.fixture
def model_config(cli_db: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("defaultProvider", "defaultModel", "defaultThinkingLevel"):
        monkeypatch.delenv(name, raising=False)
    directory = settings_module.root / "agent"
    directory.mkdir(parents=True)
    _ = (directory / "settings.json").write_text(json.dumps({
        "defaultProvider": "deepseek", "defaultModel": "test-model",
    }), encoding="utf-8")
    _ = (directory / "auth.json").write_text(json.dumps({
        "deepseek": {"type": "api_key", "key": "test-only-key"},
    }), encoding="utf-8")
    _ = (directory / "model-store.json").write_text(json.dumps({
        "deepseek": {"models": [{
            "id": "test-model", "baseUrl": "https://example.test/v1",
            "thinkingLevelMap": {"high": "max", "low": "high", "unsupported": None},
        }]},
    }), encoding="utf-8")
    return directory / "settings.json"


@pytest.fixture
def models(monkeypatch: pytest.MonkeyPatch) -> tuple[RecordingModel, RecordingModel, Mock]:
    old = RecordingModel(responses=[AIMessage(content="旧模型回答")])
    new = RecordingModel(responses=[AIMessage(content="新模型回答")])
    factory = Mock(side_effect=[old, new])
    monkeypatch.setattr(import_module("omt.ai.models.deepseek"), "init_chat_model", factory)
    return old, new, factory


def test_cli_switch_rebuilds_and_preserves_history(
    model_config: Path, models: tuple[RecordingModel, RecordingModel, Mock],
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    old, new, factory = models
    original_config = model_config.read_text()
    reader = AsyncMock(side_effect=["第一轮", " /THINKING high ", "/thinking", "第二轮", "/exit"])
    monkeypatch.setattr("omt.cli.app.read_input", reader)
    asyncio.run(run_cli())

    assert len(old.seen) == len(new.seen) == 1
    assert [message.text for message in new.seen[0] if message.type != "system"] == [
        "第一轮", "旧模型回答", "第二轮",
    ]
    assert len(asyncio.run(SessionStore(CliSettings().db_path).list_sessions(tmp_path))) == 1
    assert factory.call_count == 2
    assert "reasoning_effort" not in factory.call_args_list[0].kwargs
    assert factory.call_args_list[1].kwargs["reasoning_effort"] == "max"
    assert model_config.read_text() == original_config
    assert settings_module.load_settings().defaultThinkingLevel is None
    output = capsys.readouterr().out
    assert "实际 reasoning_effort：none" in output
    assert "实际 reasoning_effort：max" in output
    assert "可选等级：none、low、high" in output
    assert "新模型回答" in output
    assert "test-only-key" not in output


def test_switch_to_none_does_not_keep_previous_thinking_options(
    model_config: Path, models: tuple[RecordingModel, RecordingModel, Mock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = model_config.write_text(json.dumps({
        "defaultProvider": "deepseek", "defaultModel": "test-model", "defaultThinkingLevel": "high",
    }), encoding="utf-8")
    _, new, factory = models
    monkeypatch.setattr("omt.cli.app.read_input", AsyncMock(side_effect=[
        "先使用高强度", "/thinking none", "再使用 none", "/exit",
    ]))
    asyncio.run(run_cli())

    assert factory.call_count == 2
    assert factory.call_args_list[0].kwargs["reasoning_effort"] == "max"
    assert factory.call_args_list[0].kwargs["extra_body"] == {"thinking": {"type": "enabled"}}
    assert "reasoning_effort" not in factory.call_args_list[1].kwargs
    assert "extra_body" not in factory.call_args_list[1].kwargs
    assert len(new.seen) == 1


@pytest.mark.parametrize("level", ["none", "media", "unsupported"])
def test_thinking_commands_do_not_create_sessions_and_use_none(
    model_config: Path, models: tuple[RecordingModel, RecordingModel, Mock],
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, level: str,
) -> None:
    old, new, factory = models
    monkeypatch.setattr("omt.cli.app.read_input", AsyncMock(side_effect=[
        "/thinking", "/thinking high extra", f"/thinking {level}", "/exit",
    ]))
    asyncio.run(run_cli())

    assert not old.seen and not new.seen
    assert asyncio.run(SessionStore(CliSettings().db_path).list_sessions(tmp_path)) == []
    # 同一请求等级不重建；不存在的等级也必须显示实际回退结果。
    assert factory.call_count == (1 if level == "none" else 2)
    assert "reasoning_effort" not in factory.call_args.kwargs
    output = capsys.readouterr().out
    assert "用法：/thinking" in output
    assert "实际 reasoning_effort：none" in output
    if level != "none":
        assert "无有效映射" in output


@pytest.mark.parametrize("failure_stage", ["model", "agent"])
def test_switch_failure_keeps_old_manager_and_status(
    model_config: Path, models: tuple[RecordingModel, RecordingModel, Mock],
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure_stage: str,
) -> None:
    old, _, factory = models
    if failure_stage == "model":
        factory.side_effect = [old, RuntimeError("test-sensitive-sdk-error")]
    else:
        original = bootstrap.build_agent
        calls = 0

        async def build(
            model: Model, *, checkpointer: BaseCheckpointSaver[str] | None = None,
            extra_tools: Sequence[BaseTool] = (),
        ) -> Agent:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("test-sensitive-sdk-error")
            return await original(model, checkpointer=checkpointer, extra_tools=extra_tools)

        monkeypatch.setattr(bootstrap, "build_agent", build)
    monkeypatch.setattr("omt.cli.app.read_input", AsyncMock(side_effect=[
        "/thinking high", "/thinking", "继续使用旧模型", "/exit",
    ]))
    asyncio.run(run_cli())

    assert len(old.seen) == 1
    output = capsys.readouterr().out
    assert "推理强度切换失败" in output
    assert "实际 reasoning_effort：max" not in output
    assert "test-sensitive-sdk-error" not in output
    assert "旧模型回答" in output


def test_runtime_rebuild_preserves_failed_session_guard_and_closes_managers(
    model_config: Path, models: tuple[RecordingModel, RecordingModel, Mock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, new, _ = models

    async def fail_before_execution(self: Conversation, message: str) -> str:
        raise RuntimeError("模拟流创建失败，不改动已有完整检查点")

    async def execute() -> None:
        async with bootstrap.open_session_runtime(CliSettings()) as runtime:
            previous = runtime.manager
            record = await previous.create_session("失败会话")
            _ = await previous.run(record.thread_id, "先完成一轮")
            assert (await previous.get_history(record.thread_id))[-1].text == "旧模型回答"
            # 保留磁盘上的完整终态，让本例只靠内存失败标记阻止重跑。
            monkeypatch.setattr(Conversation, "arun", fail_before_execution)
            with pytest.raises(SessionError, match="本轮执行失败"):
                _ = await previous.run(record.thread_id, "触发失败")
            await runtime.set_thinking_level("high")
            current = runtime.manager
            assert current is not previous
            with pytest.raises(SessionError) as closed:
                _ = await previous.list_sessions()
            assert closed.value.code == "manager_closed"
            for operation in (current.get_history, current.open_session):
                with pytest.raises(SessionError) as incomplete:
                    _ = await operation(record.thread_id)
                assert incomplete.value.code == "session_incomplete"
            with pytest.raises(SessionError) as retry:
                _ = await current.run(record.thread_id, "不可重试")
            assert retry.value.code == "session_incomplete"
            assert not new.seen
        with pytest.raises(SessionError) as closed:
            _ = await current.list_sessions()
        assert closed.value.code == "manager_closed"
    asyncio.run(execute())


def test_switch_reuses_mcp_tools_without_rediscovery(
    model_config: Path, models: tuple[RecordingModel, RecordingModel, Mock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, new, _ = models
    invocations: list[str] = []

    @tool
    def marker() -> str:
        """只记录本地调用，不运行外部服务。"""
        invocations.append("marker")
        return "本地工具结果"

    discover = AsyncMock(return_value=[marker])
    monkeypatch.setattr(bootstrap, "get_tools", discover)
    new.responses = [
        AIMessage(content="", tool_calls=[{"name": "marker", "args": {}, "id": "marker-1"}]),
        AIMessage(content="新工具回合完成"),
    ]

    async def execute() -> None:
        options = CliSettings(mcp_servers={
            "local": StdioServerSettings(transport="stdio", command="test-not-executed", args=[]),
        })
        async with bootstrap.open_session_runtime(options) as runtime:
            await runtime.set_thinking_level("high")
            record = await runtime.manager.create_session()
            result = await runtime.manager.run(record.thread_id, "调用工具")
            assert result.answer == "新工具回合完成"
            assert invocations == ["marker"]
        discover.assert_awaited_once()
    asyncio.run(execute())


def test_switch_does_not_cancel_active_run_and_can_retry_afterwards(
    model_config: Path, models: tuple[RecordingModel, RecordingModel, Mock],
) -> None:
    _, new, factory = models

    async def execute() -> None:
        started, release = asyncio.Event(), asyncio.Event()

        class WaitingModel(RecordingModel):
            @override
            async def _agenerate(
                self, messages: list[BaseMessage], stop: list[str] | None = None,
                run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: object,
            ) -> ChatResult:
                started.set()
                await release.wait()
                return await super()._agenerate(messages, stop, run_manager, **kwargs)

        waiting = WaitingModel(responses=[AIMessage(content="完成等待")])
        factory.side_effect = [waiting, new, new]
        async with bootstrap.open_session_runtime(CliSettings()) as runtime:
            previous = runtime.manager
            record = await previous.create_session()
            running = asyncio.create_task(previous.run(record.thread_id, "等待"))
            try:
                await asyncio.wait_for(started.wait(), 5)
                with pytest.raises(SessionError) as busy:
                    await runtime.set_thinking_level("high")
                assert busy.value.code == "manager_busy"
                assert runtime.manager is previous
                assert runtime.thinking.reasoning_effort == "none"
                assert not running.done()
                release.set()
                result = await asyncio.wait_for(running, 5)
                assert result.answer == "完成等待"
                await runtime.set_thinking_level("high")
                assert runtime.manager is not previous
                result = await runtime.manager.run(record.thread_id, "继续")
                assert result.answer == "新模型回答"
            finally:
                _ = running.cancel()
                _ = await asyncio.gather(running, return_exceptions=True)
    asyncio.run(execute())


def test_cancelled_switch_keeps_old_manager_and_can_close(
    model_config: Path, models: tuple[RecordingModel, RecordingModel, Mock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old, new, factory = models
    factory.side_effect = [old, new, new]
    original = bootstrap.build_agent

    async def execute() -> None:
        started = asyncio.Event()
        calls = 0

        async def build(
            model: Model, *, checkpointer: BaseCheckpointSaver[str] | None = None,
            extra_tools: Sequence[BaseTool] = (),
        ) -> Agent:
            nonlocal calls
            calls += 1
            if calls == 2:
                started.set()
                await asyncio.Event().wait()
            return await original(model, checkpointer=checkpointer, extra_tools=extra_tools)

        monkeypatch.setattr(bootstrap, "build_agent", build)
        async with bootstrap.open_session_runtime(CliSettings()) as runtime:
            previous = runtime.manager
            switching = asyncio.create_task(runtime.set_thinking_level("high"))
            try:
                await asyncio.wait_for(started.wait(), 5)
                with pytest.raises(SessionError) as busy:
                    await runtime.set_thinking_level("low")
                assert busy.value.code == "manager_busy"
                _ = switching.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await switching
                assert runtime.manager is previous
                assert runtime.thinking.reasoning_effort == "none"
                record = await previous.create_session()
                result = await previous.run(record.thread_id, "取消切换后继续")
                assert result.answer == "旧模型回答"
                await runtime.set_thinking_level("high")
                assert runtime.manager is not previous
                assert runtime.thinking.reasoning_effort == "max"
            finally:
                _ = switching.cancel()
                _ = await asyncio.gather(switching, return_exceptions=True)
        with pytest.raises(SessionError) as closed:
            await runtime.set_thinking_level("low")
        assert closed.value.code == "manager_closed"
    asyncio.run(execute())


def test_fixed_manager_reports_unsupported_without_reading_config(
    cli_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    model = RecordingModel(responses=[AIMessage(content="未调用")])

    @asynccontextmanager
    async def factory(options: CliSettings) -> AsyncGenerator[SessionManager]:
        async with AsyncSqliteSaver.from_conn_string(str(cli_db)) as saver:
            manager = SessionManager(
                Agent(model, checkpointer=saver), SessionStore(cli_db),
                workspace_path=options.workspace_path, system_prompt="测试规则", skill_roots=[],
            )
            try:
                yield manager
            finally:
                await manager.aclose()

    monkeypatch.setattr("omt.cli.app.read_input", AsyncMock(side_effect=["/thinking high", "/exit"]))
    asyncio.run(run_cli(manager_factory=factory))
    assert not model.seen
    assert asyncio.run(SessionStore(cli_db).list_sessions(tmp_path)) == []
    assert "固定 Manager" in capsys.readouterr().out
    assert not (settings_module.root / "agent" / "settings.json").exists()


@pytest.mark.parametrize("mapping", [None, {}])
def test_startup_without_thinking_capability_shows_only_none(
    model_config: Path, models: tuple[RecordingModel, RecordingModel, Mock],
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mapping: object,
) -> None:
    _ = (model_config.parent / "model-store.json").write_text(json.dumps({
        "deepseek": {"models": [{
            "id": "test-model", "baseUrl": "https://example.test/v1", "thinkingLevelMap": mapping,
        }]},
    }), encoding="utf-8")
    monkeypatch.setattr("omt.cli.app.read_input", AsyncMock(side_effect=["/thinking", "/exit"]))
    asyncio.run(run_cli())
    assert "可选等级：none" in capsys.readouterr().out.splitlines()
    assert models[2].call_count == 1


def test_manager_rebuild_rejects_different_checkpointer(
    model_config: Path, models: tuple[RecordingModel, RecordingModel, Mock], tmp_path: Path,
) -> None:
    _, new, _ = models

    async def execute() -> None:
        async with bootstrap.open_session_runtime(CliSettings()) as runtime:
            previous = runtime.manager
            async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "other.sqlite")) as other:
                with pytest.raises(ValueError, match="同一个 Checkpointer"):
                    _ = await previous.rebuild(Agent(new, checkpointer=other))
            assert runtime.manager is previous
            assert await previous.list_sessions() == []
    asyncio.run(execute())
