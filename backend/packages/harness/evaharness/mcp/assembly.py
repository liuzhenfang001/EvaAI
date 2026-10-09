"""MCP 工具的一次运行：开连接 → 拉工具 → 关连接。

**调用方负责让这个 context 活满一次运行**。连接的作用域 = 这个 `async with` 的作用域，而
「一次运行」在 EvaAI 里由请求路径上那个 run 生成器划定（ADR-0017）。

两个不显然的地方：

**为什么要走 `async with adapter` 而不是直接 `adapter.list_tools()`**
    `list_tools()` 内部自己 `async with self`，返回的工具在**每次调用**时又各自
    `async with client`——也就是说库的默认行为是「每次工具调用建一次连接」。stdio 下那就是每个
    工具调用 spawn 一个 npx 子进程。在 run 外层显式进一次 context，工具调用内层的嵌套
    `async with client` 才会复用这条连接（fastmcp 的客户端是引用计数可重入的）。

**逐 server 隔离是我们自己加的**
    `MCPAdapter` 连接失败是直接抛的，多 server 走 `MCPConfig` 时甚至「整队共享一个协议 era」。
    要「一个 server 挂了不拖累别的」（决策 #2），唯一干净的做法就是每个 server 一个 adapter、
    各自 try/except。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager

from langchain_core.tools import BaseTool

from evaharness.config.extensions_config import McpServerConfig
from evaharness.mcp.client import make_adapter
from evaharness.mcp.naming import prefix_tool

logger = logging.getLogger(__name__)

AdapterFactory = Callable[[McpServerConfig], object]
"""吃一条 server 声明、返回一个 `MCPAdapter` 形状的对象（有 `__aenter__`/`__aexit__`/`list_tools`）。

这是**测试缝**：装配逻辑因此能在不 spawn 任何进程、不 import fastmcp 传输层的前提下测完。
"""

PROBE_TIMEOUT = 30.0
"""启动期探测的**外层**硬超时（秒），与 server 自己的 `timeout` 无关。

为什么探测要自带一个：server 自己那个 `timeout` 管的是会话内的请求，而探测要防的是
「子进程起来了却一直不说话」——那种情况下连握手都完成不了，server 自己的超时根本没机会生效。
"""


@asynccontextmanager
async def open_mcp_tools(
    servers: Mapping[str, McpServerConfig],
    *,
    adapter_factory: AdapterFactory = make_adapter,
) -> AsyncIterator[list[BaseTool]]:
    """连上所有能连的 server，产出**已改名**的工具；退出时把连接全部关掉。

    连不上或列不出工具的 server 会被跳过并记警告——它们只是「这次运行没有这些工具」，
    不是「这次运行失败」（决策 #2 / #12）。
    """
    async with AsyncExitStack() as stack:
        collected: list[BaseTool] = []
        for name, server in servers.items():
            try:
                adapter = adapter_factory(server)
                await stack.enter_async_context(adapter)  # type: ignore[arg-type]
                # cache_mode="bypass"：工具列表一次运行只取一次，缓存没有意义，只会让「配了新
                # server 却看不到」多一种解释。
                tools = await adapter.list_tools(cache_mode="bypass")  # type: ignore[attr-defined]
            except Exception:
                logger.warning(
                    "MCP server %r 连接失败，已跳过（本次运行不会有它的工具）", name, exc_info=True
                )
                continue
            collected.extend(prefix_tool(tool, name) for tool in tools)
            logger.info("MCP server %r 接上，带来 %d 个工具", name, len(tools))
        yield collected


async def probe_mcp_servers(
    servers: Mapping[str, McpServerConfig],
    *,
    adapter_factory: AdapterFactory = make_adapter,
    timeout: float = PROBE_TIMEOUT,
) -> None:
    """启动期探测：逐个连上、列一遍工具、断开、记日志。**永不抛**。

    它验的是「外部依赖现在能不能用」，与启动期 smoke build 验的「我们自己的装配连线通不通」
    是两件事——所以这里的失败是**警告**，而 smoke build 的失败**拒绝启动**（ADR-0017）。

    代价说清楚：stdio 探测会 spawn 子进程，首次 `npx` 还要下载（实测 30.9 秒）。所以每个 server
    都套了 `asyncio.timeout`，一个不响应的 server 拖不住启动。
    """
    for name, server in servers.items():
        try:
            async with asyncio.timeout(timeout):
                adapter = adapter_factory(server)
                async with adapter:  # type: ignore[attr-defined]
                    tools = await adapter.list_tools(cache_mode="bypass")  # type: ignore[attr-defined]
            logger.info(
                "MCP server %r 可用，%d 个工具：%s",
                name,
                len(tools),
                ", ".join(sorted(tool.name for tool in tools)),
            )
        except Exception as exc:
            logger.warning(
                "MCP server %r 探测失败，服务照常启动（本次运行不会有它的工具）：%s", name, exc
            )


__all__ = ["AdapterFactory", "PROBE_TIMEOUT", "open_mcp_tools", "probe_mcp_servers"]
