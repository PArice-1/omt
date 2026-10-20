"""HTTP 启动配置；会话字段复用核心配置，访问令牌只属于 HTTP。"""

from typing import ClassVar
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import SettingsConfigDict

from omt.core.configuration import SessionSettings


class ApiSettings(SessionSettings):
    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_prefix="OMT_API_", frozen=True, extra="forbid", validate_default=True,
        hide_input_in_errors=True,
    )

    # 默认空值仍经过校验，未配置令牌时启动失败，不存在匿名回退。
    skill_roots: list[Path] | None = Field(default=[Path("~/.agents/skills").expanduser().resolve()])
    access_token: SecretStr = Field(default_factory=lambda: SecretStr(""))

    @field_validator("access_token")
    @classmethod
    def check_token(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().strip()) < 32:
            raise ValueError("访问令牌至少需要 32 个非首尾空白字符")
        return value
