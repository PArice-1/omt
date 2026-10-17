"""同步 Bash 工具；使用当前用户的权限执行，不提供沙箱或执行审批。"""

import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Annotated, TypedDict

from langchain.tools import tool
from pydantic import Field

_OUTPUT_LIMIT = 20_000
TimeoutSeconds = Annotated[int, Field(strict=True, ge=1, le=120)]


class BashResult(TypedDict):
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    """终止命令所在的进程组，并回收直接子进程。"""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


@tool
def bash(
    command: str,
    cwd: str = ".",
    timeout: TimeoutSeconds = 30,
) -> BashResult:
    """在指定目录同步执行 Bash 命令，返回输出、退出码和超时状态。

    command 支持 Bash 管道、重定向和多条命令；cwd 默认是当前工作目录。
    timeout 为 1～120 秒的整数，默认 30 秒；超时会终止命令所在进程组。
    标准输入关闭，不支持交互式命令。非零退出码作为正常工具结果返回供模型分析。
    stdout/stderr 各最多返回 20000 个字符；截断发生在完整捕获之后，并非内存限制。
    仅支持安装了 Bash 的 POSIX 环境；命令拥有当前用户权限，不受目录沙箱限制。
    """
    if not command.strip():
        raise ValueError("command 不能为空")
    if "\x00" in command:
        raise ValueError("command 不能包含 NUL 字符")

    workdir = Path(cwd).expanduser().resolve()
    if not workdir.is_dir():
        raise ValueError("cwd 必须指向已存在的目录")
    if os.name != "posix":
        raise OSError("bash 工具目前仅支持 POSIX 环境")
    executable = shutil.which("bash")
    if executable is None:
        raise FileNotFoundError("未找到 Bash，请先安装并加入 PATH")

    timed_out = False
    with subprocess.Popen(
        [executable, "--noprofile", "--norc", "-c", command],
        cwd=workdir,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_group(process)
            stdout, stderr = process.communicate()
        except BaseException:
            # 调用被中断或输出读取失败时，也不能遗留本工具启动的进程组。
            _terminate_process_group(process)
            raise
        exit_code = process.wait()

    return {
        "exit_code": exit_code,
        "stdout": stdout[:_OUTPUT_LIMIT],
        "stderr": stderr[:_OUTPUT_LIMIT],
        "timed_out": timed_out,
        "truncated": len(stdout) > _OUTPUT_LIMIT or len(stderr) > _OUTPUT_LIMIT,
    }
