"""一次运行里的 MCP 工具：开连接 → 拉工具 → 交给建图 → 整轮用完再关。

**连接的作用域 = 这个 context 的作用域 = 一次运行**（决策 #3）。这就是它必须被包在
请求路径那个 run 生成器里的原因：MCP 工具要在 ``create_agent(tools=...)`` **之前**拿到手，
而连接要活到最后一帧。两个约束一夹，落点只剩一个（ADR-0017）。

放在业务层而不是 harness：**「连哪些 server」是业务决定**（写在 `extensions_config.json` 里），
`harness` 只提供「把声明变成工具」的机制（:mod:`evaharness.mcp`）。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from langchain_core.tools import BaseTool

from evaharness.agents.lead_agent import TASK_TOOL_NAME
from evaharness.config.extensions_config import McpServerConfig
from evaharness.mcp import AdapterFactory, make_adapter, open_mcp_tools

logger = logging.getLogger(__name__)


@asynccontextmanager
async def mcp_tools_for_run(
    state: Any,
    *,
    adapter_factory: AdapterFactory = make_adapter,
) -> AsyncIterator[list[BaseTool]]:
    """这次运行可用的 MCP 工具（已改名、已去掉与既有工具撞名的）。

    没配任何 server 时直接产出空表——**不建连接、不走库**，所以「没配 MCP」的运行路径与 T12
    之前完全一致（少一层开销，也少一处可能出错的代码）。

    连不上的 server 由 :func:`~evaharness.mcp.assembly.open_mcp_tools` 跳过并记警告：这次运行
    只是没有它的工具，不是这次运行失败。
    """
    servers = _configured_servers(state)
    if not servers:
        yield []
        return

    async with open_mcp_tools(servers, adapter_factory=adapter_factory) as tools:
        yield _drop_collisions(tools, taken=_taken_names(state))


def _configured_servers(state: Any) -> Mapping[str, McpServerConfig]:
    """从 `app.state` 取 server 声明；没挂（测试里的假 state）就当没配。"""
    extensions = getattr(state, "extensions_config", None)
    return getattr(extensions, "mcp_servers", None) or {}


def _taken_names(state: Any) -> set[str]:
    """已经被占掉的名字：业务工具 + 子 agent 派发工具 ``task``。"""
    factory = getattr(state, "agent_factory", None)
    business = getattr(factory, "tools", None) or []
    return {getattr(tool, "name", "") for tool in business} | {TASK_TOOL_NAME}


def _drop_collisions(tools: list[BaseTool], taken: set[str]) -> list[BaseTool]:
    """撞名的 MCP 工具**丢掉并记日志**，不抛。

    为什么与启动期那套「拒绝启动」不同：`mcp__<server>__<tool>` 是个确定性前缀，真与业务工具
    或 `task` 撞上只可能是我们自己的 bug；而按 MCP 一贯的取向（决策 #2 / #11 / #12），外部扩展
    的小故障不该把用户这一轮变成失败。丢掉的那一个会在日志里点名，不是静默的。
    """
    kept: list[BaseTool] = []
    for tool in tools:
        if tool.name in taken:
            logger.warning("MCP 工具 %r 与已有工具撞名，已丢弃", tool.name)
            continue
        kept.append(tool)
    return kept


__all__ = ["mcp_tools_for_run"]
