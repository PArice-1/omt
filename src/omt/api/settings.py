"""HTTP 启动配置；不接受请求动态更改工具目录或 MCP 命令。"""

from pathlib import Path
from typing import Annotated, ClassVar, Literal

from langchain_mcp_adapters.sessions import Connection
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from omt.coding_agent import settings


class StdioServerSettings(BaseModel):
    """只接收可从 JSON 配置的字段，不把第三方运行时 Protocol 当作配置模型。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", hide_input_in_errors=True)
    transport: Literal["stdio"]
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] | None = Field(default=None, repr=False)

    def connection(self) -> Connection:
        return {"transport": "stdio", "command": self.command, "args": self.args,
                "cwd": self.cwd, "env": self.env}


class HttpServerSettings(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", hide_input_in_errors=True)
    transport: Literal["streamable_http"]
    url: HttpUrl
    headers: dict[str, str] | None = Field(default=None, repr=False)

    def connection(self) -> Connection:
        return {"transport": "streamable_http", "url": str(self.url), "headers": self.headers}


McpServerSettings = Annotated[StdioServerSettings | HttpServerSettings, Field(discriminator="transport")]


class ApiSettings(BaseSettings):
    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_prefix="OMT_API_", frozen=True, extra="forbid", validate_default=True,
        hide_input_in_errors=True,
    )

    # 默认空值仍经过校验，未配置令牌时启动失败，不存在匿名回退。
    access_token: SecretStr = Field(default_factory=lambda: SecretStr(""))
    db_path: Path = Field(default_factory=lambda: settings.root / "agent" / "state" / "omt.sqlite")
    workspace_path: Path = Field(default_factory=Path.cwd)
    max_concurrent_runs: int = Field(default=1, ge=1)
    skill_roots: list[Path] | None = None
    mcp_servers: dict[str, McpServerSettings] = Field(default_factory=dict)

    @field_validator("access_token")
    @classmethod
    def check_token(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().strip()) < 32:
            raise ValueError("访问令牌至少需要 32 个非首尾空白字符")
        return value

    @field_validator("db_path", "workspace_path")
    @classmethod
    def normalize_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()
