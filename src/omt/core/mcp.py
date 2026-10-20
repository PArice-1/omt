from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.sessions import Connection


async def get_tools(
    servers: dict[str, Connection],
) -> list[BaseTool]:
    """异步发现 MCP 工具，连接生命周期由适配库管理。"""

    client = MultiServerMCPClient(
        servers,
        tool_name_prefix=True,
    )

    return await client.get_tools()
