"""MCP 工具在模型眼里叫什么。

**一律加前缀**（决策 #4）：`mcp__<server>__<tool>`。不这么做就会真撞名——实测官方
filesystem server 提供 `read_file` / `write_file` / `edit_file` / `search_files`，而前两个正是
我们自己的业务工具名。

为什么「始终加」而不是「撞了才加」：后者会让工具名随「今天有没有新加一个同名业务工具」而变，
而**改名是有后果的**——模型在对话里学到的名字会失效，历史 RunJournal 里记着的工具调用行也会
对不上（journal 是要能回放的老数据，不是一次性日志）。始终加，名字就是确定的。

双下划线分隔而不是单下划线：`{server}_{tool}` 在有嵌套下划线时分不清 server 名到哪结束
（`filesystem_read_file` 到底是哪个 server 的哪个工具）。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

MCP_TOOL_PREFIX = "mcp__"
"""工具名的前缀。用**双**下划线，见模块说明。"""

_SEPARATOR = "__"


def mcp_tool_name(server: str, tool: str) -> str:
    """`(server, tool)` → 模型看到的工具名。"""
    return f"{MCP_TOOL_PREFIX}{server}{_SEPARATOR}{tool}"


def prefix_tool(tool: BaseTool, server: str) -> BaseTool:
    """返回一个改了名字的**副本**——不原地改，工具实例是共享的。

    `model_copy` 是浅拷贝：`args_schema` / `coroutine` / `response_format` 原样带过去，所以
    模型看到的参数 schema 与调用路径都不变，只有名字变了。
    """
    return tool.model_copy(update={"name": mcp_tool_name(server, tool.name)})


__all__ = ["MCP_TOOL_PREFIX", "mcp_tool_name", "prefix_tool"]
