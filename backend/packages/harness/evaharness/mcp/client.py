"""把一条 server 声明变成 fastmcp 客户端 / langchain 适配器。

只做这一件事，所以测试可以整个替换掉它（`adapter_factory` 缝在 :mod:`evaharness.mcp.assembly`）。

**两个 here-be-dragons 都是实测出来的，不是照文档写的**（2026-10-08，本机 Windows 11 +
fastmcp 4.0.11）：

``keep_alive`` 必须显式关掉
    fastmcp 的 ``StdioTransport`` 默认 **True**，docstring 写的是「子进程在连接 context 退出后
    继续存活，以便后续连接复用」。在我们的每请求模型里那就是纯泄漏——实测连开三轮，默认值下
    ``node.exe`` + ``cmd.exe`` 各多两个并且**再也不还**（12→14 稳定），显式 ``keep_alive=False``
    三轮之后仍是 12。所以它不是旋钮，是硬编码。

超时给在两个地方
    ``Client(timeout=...)`` 落到会话的 ``read_timeout_seconds``；``init_timeout`` 管握手。
    不传 `init_timeout` 时它是 ``None``——**握手没有超时**，服务端不响应就是永久挂起
    （langchain#40472，OPEN）。两者共用配置里的一个 ``timeout``。

顺带：`MCPAdapter` 构造时会把客户端 ``client.new()`` 克隆一份（给 elicitation 装 interrupt）。
那是 ``copy.copy`` 浅拷贝且会复制 ``_session_kwargs``，所以上面的两个超时**在克隆后仍然有效**
（实测 ``read_timeout_seconds`` 克隆前后都是 45.0）。
"""

from __future__ import annotations

from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from langchain.mcp import MCPAdapter

from evaharness.config.extensions_config import McpServerConfig, resolve_stdio_command


def make_client(server: McpServerConfig) -> Client:
    """建一个 fastmcp 客户端。**不连接**——连接发生在 ``async with`` 那一刻。"""
    return Client(
        _make_transport(server),
        timeout=server.timeout,
        init_timeout=server.timeout,
    )


def make_adapter(server: McpServerConfig) -> MCPAdapter:
    """建一个 langchain 适配器（`:mod:`evaharness.mcp.assembly` 的缺省工厂）。"""
    return MCPAdapter(make_client(server))


def _make_transport(server: McpServerConfig) -> StdioTransport | StreamableHttpTransport:
    if server.transport == "stdio":
        # command 一定在——McpServerConfig 的 after 校验器保证 transport=stdio 必有它。
        assert server.command is not None
        return StdioTransport(
            command=resolve_stdio_command(server.command),
            args=list(server.args),
            env=server.env,
            cwd=server.cwd,
            keep_alive=False,
        )
    assert server.url is not None
    return StreamableHttpTransport(url=server.url, headers=server.headers)


__all__ = ["make_adapter", "make_client"]
