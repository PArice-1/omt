"""真实内存终端测试：保留输入原文、校验无效输入并传播 Ctrl-C/EOF。"""

import asyncio
from importlib import import_module
from unittest.mock import Mock

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.validation import ValidationError as InputValidationError

from omt.cli.terminal import (
    read_input as _aget_user_input, read_input_sync as _get_user_input,
    InputValidator as _UserInputValidator,
)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_get_user_input_uses_real_prompt_session_without_builtin_input(
    monkeypatch: pytest.MonkeyPatch, asynchronous: bool,
) -> None:
    forbidden_input = Mock(side_effect=AssertionError("不得使用内置 input"))
    monkeypatch.setattr("builtins.input", forbidden_input)
    with create_pipe_input() as pipe:
        terminal = PromptSession[str](input=pipe, output=DummyOutput())
        pipe.send_text("  你好  \n")

        result = (asyncio.run(_aget_user_input(terminal)) if asynchronous
                  else _get_user_input(terminal))

    assert result == "  你好  "
    forbidden_input.assert_not_called()


@pytest.mark.parametrize("value", ["", "   ", "\n\t", "hello\x00world"])
def test_input_validator_rejects_blank_or_nul_input(value: str) -> None:
    with pytest.raises(InputValidationError):
        _UserInputValidator().validate(Document(value))


def test_input_validator_accepts_unicode_and_multiline() -> None:
    _UserInputValidator().validate(Document("分析这个项目\n并列出问题"))


@pytest.mark.parametrize("asynchronous", [False, True])
def test_prompt_rejects_blank_line_then_accepts_next_input(asynchronous: bool) -> None:
    with create_pipe_input() as pipe:
        terminal = PromptSession[str](input=pipe, output=DummyOutput())
        pipe.send_text("\n你好\n")

        result = (asyncio.run(_aget_user_input(terminal)) if asynchronous
                  else _get_user_input(terminal))
        assert result == "你好"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(("control", "exception"), [("\x03", KeyboardInterrupt), ("\x04", EOFError)])
def test_prompt_propagates_cancel_and_eof(
    control: str, exception: type[BaseException], asynchronous: bool,
) -> None:
    with create_pipe_input() as pipe:
        terminal = PromptSession[str](input=pipe, output=DummyOutput())
        pipe.send_text(control)

        with pytest.raises(exception):
            if asynchronous:
                asyncio.run(_aget_user_input(terminal))
            else:
                _get_user_input(terminal)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_get_user_input_can_create_its_own_prompt_session(
    monkeypatch: pytest.MonkeyPatch, asynchronous: bool,
) -> None:
    module = import_module("omt.cli.terminal.plain")
    with create_pipe_input() as pipe:
        terminal = PromptSession[str](input=pipe, output=DummyOutput())
        factory = Mock(return_value=terminal)
        monkeypatch.setattr(module, "PromptSession", factory)
        pipe.send_text("独立读取输入\n")

        result = (asyncio.run(_aget_user_input()) if asynchronous else _get_user_input())
        assert result == "独立读取输入"

    factory.assert_called_once_with()
