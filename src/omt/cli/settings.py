"""CLI 启动配置，与 HTTP 使用相同会话字段，但不需要访问令牌。"""

from pathlib import Path
from typing import ClassVar

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from omt.core.configuration import SessionSettings


class CliSettings(SessionSettings):
    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_prefix="OMT_CLI_", frozen=True, extra="forbid", hide_input_in_errors=True,
    )
    skill_roots: list[Path] | None = Field(default=[Path("~/.agents/skills").expanduser().resolve()])
