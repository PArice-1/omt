"""Coding Agent 测试共享 fixture（本文件不包含测试用例）。

non_regular_file：在 tmp_path 中创建 FIFO，用于验证 read/edit/write 拒绝非普通文件。
不支持 os.mkfifo 的平台自动跳过依赖此 fixture 的用例；不打开或读写 FIFO。
"""

import os
from pathlib import Path

import pytest


@pytest.fixture
def non_regular_file(tmp_path: Path) -> Path:
    if not hasattr(os, "mkfifo"):
        pytest.skip("当前平台不支持创建 FIFO")
    file = tmp_path / "pipe"
    os.mkfifo(file)
    return file
