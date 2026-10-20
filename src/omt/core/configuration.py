"""CLI 与 HTTP 共用的会话启动字段；各入口设置自己的环境变量前缀。"""

from pathlib import Path
from typing import Annotated, ClassVar, Literal

from langchain_mcp_adapters.sessions import Connection
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from omt.core import settings


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


class SessionSettings(BaseSettings):
    """会话组装配置，不包含 HTTP 访问凭据。

    db_path 指向预建索引与检查点数据库；workspace_path 只声明工作目录，
    不执行 chdir。skill_roots=None 使用用户/项目默认目录，[] 关闭扫描。
    mcp_servers 只接受可信的启动配置；max_concurrent_runs 限制运行数而非工具数。
    """

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True,
    )
    db_path: Path = Field(default_factory=lambda: settings.root / "agent" / "state" / "omt.sqlite")
    workspace_path: Path = Field(default_factory=Path.cwd)
    max_concurrent_runs: int = Field(default=1, ge=1)
    skill_roots: list[Path] | None = None
    mcp_servers: dict[str, McpServerSettings] = Field(default_factory=dict)

    @field_validator("db_path", "workspace_path")
    @classmethod
    def normalize_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()
