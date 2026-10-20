"""Bash 工具测试：只执行本地测试命令，不联网、不读取凭据。"""

import json
import os
import shlex
import shutil
import sys
from importlib import import_module
from pathlib import Path
from time import monotonic
from unittest.mock import Mock

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import ValidationError

from omt.agent.agent import Agent
from omt.coding_agent.conversation import Conversation
from omt.cli.terminal import print_event
from omt.coding_agent.tools.bash import bash
from omt.coding_agent.tools_manager import load_tools

pytestmark = pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None,
    reason="进程组测试要求 POSIX 环境及 Bash",
)


def test_bash_captures_stdout_stderr_and_nonzero_exit_code(tmp_path: Path):
    result = bash.invoke({
        "command": "printf '正常输出'; printf '错误输出' >&2; exit 7",
        "cwd": str(tmp_path),
    })

    assert result == {
        "exit_code": 7,
        "stdout": "正常输出",
        "stderr": "错误输出",
        "timed_out": False,
        "truncated": False,
    }


def test_bash_defaults_to_current_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)

    result = bash.invoke({"command": "pwd"})

    assert result["exit_code"] == 0
    assert result["stdout"].strip() == str(tmp_path)


def test_bash_handles_working_directory_with_spaces_and_unicode(tmp_path: Path):
    directory = tmp_path / "测试 目录"
    directory.mkdir()

    result = bash.invoke({"command": "pwd", "cwd": str(directory)})

    assert result["stdout"].strip() == str(directory)


def test_bash_supports_pipes_and_redirection(tmp_path: Path):
    result = bash.invoke({
        "command": "printf 'hello\\nworld\\n' | tr '[:lower:]' '[:upper:]' > result.txt; cat result.txt",
        "cwd": str(tmp_path),
    })

    assert result["exit_code"] == 0
    assert result["stdout"] == "HELLO\nWORLD\n"
    assert (tmp_path / "result.txt").read_text() == "HELLO\nWORLD\n"


@pytest.mark.parametrize("command", ["", "   ", "\n\t", "hello\x00world"])
def test_invalid_command_is_rejected(command: str, tmp_path: Path):
    with pytest.raises(ValueError, match="command"):
        bash.invoke({"command": command, "cwd": str(tmp_path)})


@pytest.mark.parametrize("timeout", [0, -1, 121, 1.5, True, "not-a-number"])
def test_invalid_timeout_is_rejected(timeout, tmp_path: Path):
    with pytest.raises(ValidationError):
        bash.invoke({"command": "printf '不应执行'", "cwd": str(tmp_path), "timeout": timeout})


def test_missing_working_directory_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="cwd"):
        bash.invoke({"command": "pwd", "cwd": str(tmp_path / "missing")})


def test_file_cannot_be_used_as_working_directory(tmp_path: Path):
    file = tmp_path / "not-a-directory"
    file.write_text("测试")

    with pytest.raises(ValueError, match="cwd"):
        bash.invoke({"command": "pwd", "cwd": str(file)})


def test_timeout_terminates_process_group_and_keeps_partial_output(tmp_path: Path):
    started = monotonic()
    result = bash.invoke({
        "command": "printf '执行开始\\n'; sleep 30 & wait",
        "cwd": str(tmp_path),
        "timeout": 1,
    })
    elapsed = monotonic() - started

    assert result["timed_out"] is True
    assert result["exit_code"] != 0
    assert result["stdout"] == "执行开始\n"
    # 如果只杀死 shell，后台 sleep 会持有输出管道，communicate 将继续阻塞。
    assert elapsed < 5


def test_bash_does_not_read_from_interactive_stdin(tmp_path: Path):
    result = bash.invoke({"command": "read -r value", "cwd": str(tmp_path), "timeout": 1})

    assert result["timed_out"] is False
    assert result["exit_code"] == 1


def test_large_stdout_and_stderr_are_truncated_as_text(tmp_path: Path):
    script = "import sys; sys.stdout.write('中' * 20005); sys.stderr.write('错' * 20005)"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    result = bash.invoke({"command": command, "cwd": str(tmp_path)})

    assert result["exit_code"] == 0
    assert result["stdout"] == "中" * 20000
    assert result["stderr"] == "错" * 20000
    assert result["truncated"] is True


def test_output_at_limit_is_not_marked_as_truncated(tmp_path: Path):
    script = "print('x' * 20000, end='')"
    result = bash.invoke({
        "command": f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}",
        "cwd": str(tmp_path),
    })

    assert len(result["stdout"]) == 20000
    assert result["truncated"] is False


def test_non_utf8_output_is_replaced_instead_of_raising(tmp_path: Path):
    result = bash.invoke({"command": "printf '\\377'", "cwd": str(tmp_path)})

    assert result["stdout"] == "\ufffd"
    assert result["exit_code"] == 0


def test_shell_syntax_error_is_returned_to_model(tmp_path: Path):
    result = bash.invoke({"command": "if", "cwd": str(tmp_path)})

    assert result["exit_code"] == 2
    assert result["stderr"]
    assert result["timed_out"] is False


def test_missing_bash_executable_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = import_module("omt.coding_agent.tools.bash")
    monkeypatch.setattr(module.shutil, "which", Mock(return_value=None))

    with pytest.raises(FileNotFoundError, match="Bash"):
        bash.invoke({"command": "pwd", "cwd": str(tmp_path)})


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, RuntimeError])
def test_interrupted_output_capture_terminates_the_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exception_type
):
    module = import_module("omt.coding_agent.tools.bash")
    error = exception_type("模拟输出读取中断")
    processes = []

    def interrupted(process, *args, **kwargs):
        processes.append(process)
        raise error

    monkeypatch.setattr(module.subprocess.Popen, "communicate", interrupted)

    with pytest.raises(exception_type) as raised:
        bash.invoke({"command": "sleep 30", "cwd": str(tmp_path)})

    assert raised.value is error
    assert len(processes) == 1
    assert processes[0].poll() is not None


def test_registered_bash_runs_in_agent_and_preserves_tool_message(tmp_path: Path, capsys):
    class LocalBashModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    model = LocalBashModel(responses=[
        AIMessage(content="我先执行命令。", tool_calls=[{
            "name": "bash",
            "args": {"command": "printf '命令执行成功'", "cwd": str(tmp_path)},
            "id": "local-bash-session",
        }]),
        AIMessage(content="命令已完成。"),
    ])
    session = Conversation(Agent(model, tools=load_tools()), event_handler=print_event)

    reply = session.run("执行本地测试命令")

    assert reply == "命令已完成。"
    assert [message.type for message in session.messages] == ["human", "ai", "tool", "ai"]
    tool_message = session.messages[2]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "local-bash-session"
    assert json.loads(tool_message.text)["stdout"] == "命令执行成功"
    output = capsys.readouterr().out
    assert "[工具开始] bash" in output
    assert "[工具结果] bash" in output
    assert "[工具完成] bash" in output


def test_tool_call_returns_tool_message_with_structured_result(tmp_path: Path):
    result = bash.invoke({
        "type": "tool_call",
        "name": "bash",
        "id": "bash-local-call",
        "args": {"command": "printf '工具结果'", "cwd": str(tmp_path)},
    })

    assert isinstance(result, ToolMessage)
    assert result.tool_call_id == "bash-local-call"
    payload = json.loads(result.text)
    assert payload["stdout"] == "工具结果"
    assert payload["exit_code"] == 0
