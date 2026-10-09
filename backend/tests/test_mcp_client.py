"""`make_client` 的契约：三个**实测出来的**坑各有一条护栏。

这些值的来源都是本机探针，不是文档（fastmcp 4.0.11）。改它们之前先看
``evaharness/mcp/client.py`` 的模块说明——每一条背后都有一个具体的失败现象。
"""

from __future__ import annotations

import pytest
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport

from evaharness.config.extensions_config import McpServerConfig
from evaharness.mcp import make_client


def test_stdio_never_keeps_the_subprocess_alive():
    """`keep_alive` 必须是 False。

    fastmcp 的默认是 **True**（「子进程在连接退出后继续存活以便复用」）。实测：那个默认值下连开
    三轮，``node.exe`` + ``cmd.exe`` 各多两个并且**再也不还**（12 → 14，稳定在 14）；显式 False
    则三轮之后进程数不变。它不是旋钮——连接的作用域 = 一次运行，子进程必须跟着走。
    """
    client = make_client(McpServerConfig(command="npx.cmd", args=["-y", "pkg"]))
    assert isinstance(client.transport, StdioTransport)
    assert client.transport.keep_alive is False


def test_stdio_passes_the_arguments_and_environment_through():
    client = make_client(
        McpServerConfig(command="npx.cmd", args=["-y", "pkg", "/tmp"], env={"A": "b"}, cwd="/tmp")
    )
    transport = client.transport
    assert transport.args == ["-y", "pkg", "/tmp"]
    assert transport.env == {"A": "b"}
    assert transport.cwd == "/tmp"


def test_stdio_resolves_the_command_to_something_executable():
    """Windows 上 `npx` 必须被解成 `npx.cmd`，否则 CreateProcess 报 WinError 193。

    `resolve_stdio_command` 自己的规则在 `test_extensions_config.py` 里测；这里只钉「建客户端
    的那条路真的走了它」。
    """
    client = make_client(McpServerConfig(command="npx.cmd"))
    assert str(client.transport.command).lower().endswith(".cmd")


def test_both_timeouts_are_set_and_shared():
    """读超时与握手超时**都必须给**。

    stdio 不响应时 langchain#40472（OPEN）记的行为是**永久挂起**——而 fastmcp 的
    ``read_timeout_seconds`` 与 ``_init_timeout`` 不传就都是 ``None``。一个配置字段管两处。
    """
    client = make_client(McpServerConfig(command="npx.cmd", timeout=42))
    assert client._session_kwargs["read_timeout_seconds"] == 42
    assert client._init_timeout == 42


def test_http_uses_the_streamable_transport():
    """决策 #6：http 走 streamable-http（`sse` 与 `websocket` 不在范围里）。"""
    client = make_client(McpServerConfig(url="https://example.com/mcp", headers={"A": "b"}))
    assert isinstance(client.transport, StreamableHttpTransport)
    assert str(client.transport.url) == "https://example.com/mcp"
    assert client.transport.headers == {"A": "b"}


def test_an_http_client_gets_the_timeouts_too():
    client = make_client(McpServerConfig(url="https://example.com/mcp", timeout=7))
    assert client._session_kwargs["read_timeout_seconds"] == 7
    assert client._init_timeout == 7


def test_the_client_is_not_connected_by_construction():
    """建客户端**不开连接**——连接发生在 `async with` 那一刻（`open_mcp_tools` 负责）。

    这条挡的是「有人顺手在 make_client 里做一次握手」：那会把「启动期探测」与「每次运行建连」
    两件事搅在一起，而它们的失败语义刚好相反（一个只警告、一个照常跑）。
    """
    client = make_client(McpServerConfig(command="definitely-not-a-real-command-xyz"))
    assert client.transport.command == "definitely-not-a-real-command-xyz"


@pytest.mark.parametrize("timeout", [5, 60])
def test_the_timeout_comes_from_the_config_not_a_constant(timeout: int):
    assert make_client(McpServerConfig(command="npx.cmd", timeout=timeout))._init_timeout == timeout
