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
from evaharness.mcp.naming import mcp_tool_name, prefix_tool

logger = logging.getLogger(__name__)

AdapterFactory = Callable[[McpServerConfig], object]
"""吃一条 server 声明、返回一个 `MCPAdapter` 形状的对象（有 `__aenter__`/`__aexit__`/`list_tools`）。

这是**测试缝**：装配逻辑因此能在不 spawn 任何进程、不 import fastmcp 传输层的前提下测完。
"""

PROBE_TIMEOUT = 60.0
"""启动期探测的**外层**硬超时（秒），与 server 自己的 `timeout` 无关。

为什么探测要自带一个：server 自己那个 `timeout` 管的是会话内的请求，而探测要防的是
「子进程起来了却一直不说话」——那种情况下连握手都完成不了，server 自己的超时根本没机会生效。

**为什么是 60 而不是 30**：实测第一次 `npx` 起 filesystem server 要 **30.9 秒**（那一次要把包
下下来）。30 秒卡在它下面，于是「刚配好、第一次启动」必然超时报警，而之后再启动又一切正常——
那种「只错一次、查不到原因」的症状最难对付。冷启动之后它稳定在 2–3 秒。
"""


def _active_servers(servers: Mapping[str, McpServerConfig]) -> dict[str, McpServerConfig]:
    """挑出**要连**的 server，并说出跳过了哪些。

    `enabled: false` 的在这里就被摘掉——它**根本不进连接那一段**，所以既不 spawn 子进程、
    也不占启动时间。两个入口（启动期装载 / 一次运行的 context）都走这一关，免得「换一条路
    就连上了禁用的 server」。
    """
    skipped = [name for name, server in servers.items() if not server.enabled]
    if skipped:
        logger.info("MCP server 已禁用（extensions_config.json 里 enabled=false），跳过：%s", "、".join(skipped))
    return {name: server for name, server in servers.items() if server.enabled}


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
        for name, server in _active_servers(servers).items():
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


async def load_mcp_tools(
    servers: Mapping[str, McpServerConfig],
    *,
    adapter_factory: AdapterFactory = make_adapter,
    timeout: float = PROBE_TIMEOUT,
) -> list[BaseTool]:
    """**启动期**逐个连上、把工具列表取回来留着，然后立刻断开。**永不抛。**

    这就是「加 MCP」的全部运行时成本所在：之后每次运行都用这份清单，**不再连任何东西**；
    模型真的调某个工具时，工具内部才自己建连（`as_langchain_tool` 的 `call_tool` 里那个
    `async with client`）。

    为什么这样分：工具在模型眼里的样子（`name` / `description` / `args_schema`）是**数据**，
    建图时随 `bind_tools` 发出去，**不需要连接**；连接只在**执行**时才需要。实测（2026-10-10）：
    连接关掉之后工具对象照样带着完整的 schema，5 路并发调用全部成功，且进程数不累积。

    连不上的 server 只记警告、不拦启动（决策 #2），它带来的工具这次进程生命周期内就不会有——
    想让它生效要重启。与启动期 smoke build 的分工照旧：smoke 验**我们自己的装配**（错了拒绝启动），
    这里验**外部依赖现在能不能用**（错了只警告）。

    代价说清楚：stdio 会 spawn 子进程，首次 `npx` 还要下载（实测 30.9 秒），所以每个 server 都套了
    `asyncio.timeout`，一个不响应的拖不住启动。
    """
    collected: list[BaseTool] = []
    for name, server in _active_servers(servers).items():
        try:
            async with asyncio.timeout(timeout):
                adapter = adapter_factory(server)
                async with adapter:  # type: ignore[attr-defined]
                    tools = await adapter.list_tools(cache_mode="bypass")  # type: ignore[attr-defined]
            collected.extend(prefix_tool(tool, name) for tool in tools)
            logger.info(
                "MCP server %r 接上了，%d 个工具：%s",
                name,
                len(tools),
                ", ".join(sorted(mcp_tool_name(name, tool.name) for tool in tools)),
            )
        except TimeoutError:
            # `asyncio.timeout` 抛的 `TimeoutError` 的 str() 是**空的**——不单独接住的话，日志会
            # 打成「失败：TimeoutError」，既不说超时、也不说可能只是在下包（实测第一次 npx
            # 要 30.9 秒）。
            logger.warning(
                "MCP server %r 连接超时（%.0f 秒内没连上），服务照常启动，本次不会有它的工具。"
                "如果是第一次用它，可能只是在下包或编译——再启动一次通常就好了；"
                "真要更久就在 extensions_config.json 里给它加 `timeout`。",
                name,
                timeout,
            )
        except Exception as exc:
            logger.warning(
                "MCP server %r 连接失败，服务照常启动，本次不会有它的工具：%s", name, exc
            )
    # 排序：让这次进程生命周期里交出去的顺序是确定的。工具定义会随请求发给模型，顺序抖动会让
    # 前缀缓存白废（与技能清单排序同一个理由）。
    return sorted(collected, key=lambda tool: tool.name)


__all__ = ["AdapterFactory", "PROBE_TIMEOUT", "load_mcp_tools", "open_mcp_tools"]
