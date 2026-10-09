"""MCP 工具的**启动期装载**：把 `extensions_config.json` 里的 server 变成能用的工具。

**连接只在启动期发生一次**。之后每次运行都用启动期拿到的这份工具清单，一个字节的 I/O 都没有；
模型真的调某个工具时，工具内部才自己建连（`as_langchain_tool` 的 `call_tool`）。

为什么能这么分：工具在模型眼里的样子（`name` / `description` / `args_schema`）是**数据**——建图时
随 `bind_tools` 发出去，**不需要连接**；连接只在**执行**时才需要。实测（2026-10-10）：连接关掉之后
工具对象照样带着完整 schema，5 路并发调用全部成功，进程数也不累积。

代价照实说：**没连上的 server，这次进程生命周期内就不会有它的工具**（想让它生效要重启）；而连上了
之后又挂掉的 server，工具**仍在列表里**，报错会发生在**调用那一刻**（由
:class:`~evaharness.agents.middlewares.tool_error_handling.ToolErrorHandlingMiddleware` 转成一条
`status="error"` 的工具消息给模型看）。

放在业务层而不是 harness：**「连哪些 server」是业务决定**（写在 `extensions_config.json` 里），
harness 只提供「把声明变成工具」的机制（:mod:`evaharness.mcp`）。
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping

from langchain_core.tools import BaseTool

from evaharness.config.extensions_config import McpServerConfig
from evaharness.mcp import AdapterFactory, make_adapter
from evaharness.mcp import load_mcp_tools as _fetch_mcp_tools

logger = logging.getLogger(__name__)


async def load_mcp_tools(
    servers: Mapping[str, McpServerConfig],
    *,
    business_tools: Collection[BaseTool],
    adapter_factory: AdapterFactory = make_adapter,
) -> list[BaseTool]:
    """连一次、取回全部工具、丢掉与既有工具撞名的那些，返回**能用的**那批。

    调用方（lifespan）把它接到 `AgentFactory` 的工具表上——从那一刻起，MCP 工具与业务工具在
    建图眼里没有区别，于是主图、子图、Plan-Exec 执行单元**自动**都拿到它们，不需要三条路径各写
    一遍（T13 的 `extra_tools` 就是为那个「每请求才有」的语义存在的，现在 MCP 工具是启动期常量，
    那一层就没必要了）。
    """
    tools = await _fetch_mcp_tools(servers, adapter_factory=adapter_factory)
    return _drop_collisions(tools, taken={tool.name for tool in business_tools})


def _drop_collisions(tools: list[BaseTool], taken: set[str]) -> list[BaseTool]:
    """与既有工具**逐字同名**的 MCP 工具：丢掉并记日志，不抛。

    **真正挡住撞名的是前缀，不是这里**：server 提供一个叫 `read_file` 的工具，装配会把它变成
    `mcp__fs__read_file`，压根碰不到我们的 `read_file`；同理它也**永远不可能等于** `task`
    （带前缀的名字不可能是裸名）。实测这不是假想——官方 filesystem server 就提供 `read_file` /
    `write_file`，正是我们的工具名。

    所以这里挡的是一个**残留角落**：业务工具自己就叫 `mcp__fs__read_file`（配置里手写的）。它不
    可能发生，但一旦发生，症状是模型收到两份同名 schema、调用落到哪一个不确定——正是 ADR-0011
    记的 `create_agent` 静默盲区。守它的成本是几行，代价不对称。
    """
    kept: list[BaseTool] = []
    for tool in tools:
        if tool.name in taken:
            logger.warning("MCP 工具 %r 与已有工具逐字同名，已丢弃", tool.name)
            continue
        kept.append(tool)
    return kept


__all__ = ["load_mcp_tools"]
