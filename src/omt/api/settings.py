"""HTTP 启动配置；会话字段复用核心配置，访问令牌只属于 HTTP。"""

from typing import ClassVar
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from omt.core.configuration import SessionSettings


class ApiSettings(SessionSettings):
    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_prefix="OMT_API_", frozen=True, extra="forbid", validate_default=True,
        hide_input_in_errors=True,
    )

    # 当前仅可信本机使用，access_token 保留兼容但不启用鉴权。
    skill_roots: list[Path] | None = Field(default=[Path("~/.agents/skills").expanduser().resolve()])
    access_token: SecretStr = Field(default_factory=lambda: SecretStr(""))

    # SSE 单条/积压按编码后字节计量；到达上限中止，不截断正文。
    stream_buffer_events: int = Field(default=256, ge=1)
    stream_buffer_bytes: int = Field(default=1048576, ge=1024)
    stream_send_timeout: float = Field(default=30.0, gt=0)
    stream_heartbeat: float = Field(default=15.0, gt=0)
    # 配置预览含凭据，只在当前进程内存保留；查询中的预览也占名额。
    login_preview_ttl: int = Field(default=600, ge=1)
    login_preview_limit: int = Field(default=8, ge=1)
