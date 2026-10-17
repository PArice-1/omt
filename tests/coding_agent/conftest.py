"""Coding Agent 测试共享 fixture（本文件不包含测试用例）。

non_regular_file：在 tmp_path 中创建 FIFO，用于验证 read/edit/write 拒绝非普通文件。
不支持 os.mkfifo 的平台自动跳过依赖此 fixture 的用例；不打开或读写 FIFO。
所有配置读取与工作目录强制定位到 tmp_path，避免误读用户凭据和默认 Skills。
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from omt.coding_agent import settings as settings_module


@pytest.fixture(autouse=True)
def isolated_config_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    monkeypatch.setattr(settings_module, "root", tmp_path)
    monkeypatch.chdir(tmp_path)
    settings_module.load_settings.cache_clear()
    try:
        yield
    finally:
        settings_module.load_settings.cache_clear()


@pytest.fixture
def non_regular_file(tmp_path: Path) -> Path:
    if not hasattr(os, "mkfifo"):
        pytest.skip("当前平台不支持创建 FIFO")
    file = tmp_path / "pipe"
    os.mkfifo(file)
    return file
