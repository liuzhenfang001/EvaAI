"""MCP 集成：把 `mcpServers` 声明变成 agent 能用的工具。

分三层，各自可单独测：

- :mod:`~evaharness.mcp.naming` —— 工具在模型眼里叫什么（纯函数）。
- :mod:`~evaharness.mcp.client` —— 声明 → fastmcp 客户端（唯一 import fastmcp 传输层的地方）。
- :mod:`~evaharness.mcp.assembly` —— 连接的开与关、坏 server 的隔离（`adapter_factory` 缝）。

配置的 typed 契约在 :mod:`evaharness.config.extensions_config`（那里不 import fastmcp）。
"""

from __future__ import annotations

from evaharness.mcp.assembly import PROBE_TIMEOUT, AdapterFactory, open_mcp_tools, probe_mcp_servers
from evaharness.mcp.client import make_adapter, make_client
from evaharness.mcp.naming import MCP_TOOL_PREFIX, mcp_tool_name, prefix_tool

__all__ = [
    "MCP_TOOL_PREFIX",
    "PROBE_TIMEOUT",
    "AdapterFactory",
    "make_adapter",
    "make_client",
    "mcp_tool_name",
    "open_mcp_tools",
    "prefix_tool",
    "probe_mcp_servers",
]
