"""app 层的 MCP **启动期装载**：撞名过滤 + 坏 server 降级，以及「运行期不碰 MCP」。

装载发生在 lifespan 里（`app.gateway.app`），之后工具就跟业务工具一样躺在
`AgentFactory.tools` 里——**每次运行零 I/O**。最后那条 `test_the_request_path_does_not_touch_mcp`
就是在守这件事：它是这次改动的**全部意义**，而它坏掉的方式恰恰是静默的（多一次连、没人会报错，
只是每条消息慢 2 秒）。
"""

from __future__ import annotations

import pytest
from langchain_core.tools import BaseTool, tool

from app.gateway.mcp import load_mcp_tools
from evaharness.agents.lead_agent import TASK_TOOL_NAME
from evaharness.config.extensions_config import McpServerConfig


@tool("read_file")
def _server_read_file(path: str) -> str:
    """服务端给的原名——它会与我们的业务工具撞名。"""
    return path


@tool("echo")
def _server_echo(text: str) -> str:
    """一个不撞名的。"""
    return text


@tool(TASK_TOOL_NAME)
def _server_task(prompt: str) -> str:
    """服务端竟然有个叫 task 的——撞派发子 agent 的保留名。"""
    return prompt


class FakeAdapter:
    def __init__(self, log: list[str], *, fail: bool = False) -> None:
        self.log = log
        self.fail = fail

    async def __aenter__(self) -> FakeAdapter:
        self.log.append("enter")
        if self.fail:
            raise RuntimeError("这个 server 起不来")
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.log.append("exit")

    async def list_tools(self, *, cache_mode: str = "use") -> list[BaseTool]:
        self.log.append("list")
        return [_server_read_file, _server_echo, _server_task]


def _server(name: str) -> McpServerConfig:
    return McpServerConfig(command=name)


@pytest.mark.asyncio
async def test_tools_are_prefixed_and_handed_over(caplog):
    caplog.set_level(20)  # INFO：成功装载要看得见，否则「接上了没」只能靠猜
    log: list[str] = []
    tools = await load_mcp_tools(
        {"fs": _server("fs")}, business_tools=[], adapter_factory=lambda _s: FakeAdapter(log)
    )

    assert sorted(t.name for t in tools) == [
        "mcp__fs__echo",
        "mcp__fs__read_file",
        "mcp__fs__task",
    ]
    assert "fs" in caplog.text


@pytest.mark.asyncio
async def test_a_server_tool_named_read_file_does_not_shadow_ours():
    """**前缀才是挡撞名的那道墙。**

    实测这不是假想：官方 filesystem server 就提供 `read_file` / `write_file`，正是我们的工具名。
    没有前缀的话模型会收到两份同名 schema、调用落到哪一个不确定（`create_agent` 对此静默接受，
    ADR-0011 记的盲区）；加了前缀，两个工具**都在**、互不干扰。
    """

    @tool("read_file")
    def our_read_file(path: str) -> str:
        """我们自己的。"""
        return path

    tools = await load_mcp_tools(
        {"fs": _server("fs")},
        business_tools=[our_read_file],
        adapter_factory=lambda _s: FakeAdapter([]),
    )

    names = {t.name for t in tools}
    assert "mcp__fs__read_file" in names, "加了前缀就碰不到我们的 read_file，该照常保留"


def test_a_prefixed_mcp_name_can_never_equal_a_reserved_bare_name():
    """不变量：MCP 工具名永远带 `mcp__` 前缀，所以**永远不可能**等于 `task` 这类裸名。

    这条解释一处**刻意的不作为**：`_drop_collisions` 不把 `TASK_TOOL_NAME` 放进 taken——带前缀的名字
    撞不上它，加了也是不可达的死代码。谁要是把前缀去掉（比如改成「撞了才加」），这条会红。
    """
    from evaharness.mcp import MCP_TOOL_PREFIX, mcp_tool_name

    for bare in ("read_file", TASK_TOOL_NAME, "task", "task_list"):
        name = mcp_tool_name("fs", bare)
        assert name.startswith(MCP_TOOL_PREFIX)
        assert name != TASK_TOOL_NAME


@pytest.mark.asyncio
async def test_a_business_tool_that_literally_uses_the_prefix_loses(caplog):
    """残留角落：业务工具**自己**就叫 `mcp__fs__echo`（配置里手写的）。

    不可能发生，但真发生了就是两份同名 schema。守它的成本是几行，而代价不对称——所以丢掉 MCP
    那一个，并**响亮**地说出来。
    """

    @tool("mcp__fs__echo")
    def weird(text: str) -> str:
        """业务工具，但名字带 MCP 前缀。"""
        return text

    tools = await load_mcp_tools(
        {"fs": _server("fs")},
        business_tools=[weird],
        adapter_factory=lambda _s: FakeAdapter([]),
    )

    assert "mcp__fs__echo" not in {t.name for t in tools}
    assert "mcp__fs__echo" in caplog.text


@pytest.mark.asyncio
async def test_a_broken_server_does_not_take_down_the_others(caplog):
    log: list[str] = []
    tools = await load_mcp_tools(
        {"bad": _server("bad"), "ok": _server("ok")},
        business_tools=[],
        adapter_factory=lambda s: FakeAdapter(log, fail=s.command == "bad"),
    )

    assert any(t.name.startswith("mcp__ok__") for t in tools)
    assert not any(t.name.startswith("mcp__bad__") for t in tools)
    assert "bad" in caplog.text


@pytest.mark.asyncio
async def test_the_connection_is_released_before_the_tools_are_returned():
    """装载完就把连接还回去——之后工具自己按需建连（实测：连接关掉后调用照样成功）。"""
    log: list[str] = []
    await load_mcp_tools(
        {"fs": _server("fs")}, business_tools=[], adapter_factory=lambda _s: FakeAdapter(log)
    )

    assert log == ["enter", "list", "exit"]


def test_the_request_path_does_not_touch_mcp():
    """**每次运行零 MCP I/O**——这是这次改动的全部意义，而它坏掉的方式是静默的。

    谁把「按请求装载 MCP」重新加回路由，这里就红。MCP 工具在启动期就已经躺在
    `AgentFactory.tools` 里了，路由不需要知道它们的存在。
    """
    import app.gateway.routers.chat as chat

    for name in ("mcp_tools_for_run", "load_mcp_tools", "open_mcp_tools"):
        assert not hasattr(chat, name), f"路由里不该出现 {name}——运行期不该碰 MCP"
