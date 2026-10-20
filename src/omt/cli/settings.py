"""CLI 启动配置，与 HTTP 使用相同会话字段，但不需要访问令牌。"""

from pathlib import Path
from typing import ClassVar

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from omt.core.configuration import McpServerSettings, SessionSettings, StdioServerSettings


def _default_mcp_servers() -> dict[str, McpServerSettings]:
    """保留旧 CLI 的 knowledge 连接；环境变量可替换，{} 可显式关闭。

    这是既有的本机部署路径，不是可移植默认值；HTTP 不继承此配置。
    """
    root = Path("/home/laterain/Projects/Agent/rag-mcp")
    return {"knowledge": StdioServerSettings(
        transport="stdio", command=str(root / ".venv" / "bin" / "python"),
        args=["-m", "rag_mcp.server"], cwd=str(root),
    )}


class CliSettings(SessionSettings):
    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_prefix="OMT_CLI_", frozen=True, extra="forbid", hide_input_in_errors=True,
    )
    skill_roots: list[Path] | None = Field(default=[Path("~/.agents/skills").expanduser().resolve()])
    mcp_servers: dict[str, McpServerSettings] = Field(default_factory=_default_mcp_servers)
