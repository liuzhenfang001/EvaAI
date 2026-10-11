"""MCP 装配的契约：连接怎么开、怎么关、坏 server 怎么办。

**全程不连真 server**——`adapter_factory` 是这条缝，装配逻辑因此能在常规套件里跑。
真连一次的成本是几十秒（首次 npx 还要下包），那是端到端的事，不是单测的事。
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from langchain_core.tools import BaseTool, tool

from evaharness.config.extensions_config import McpServerConfig
from evaharness.mcp import load_mcp_tools, mcp_tool_name, open_mcp_tools, prefix_tool


@tool
def read_file(path: str) -> str:
    """Read a file from disk."""
    return f"contents of {path}"


@tool
def list_directory(path: str) -> str:
    """List a directory."""
    return path


class FakeAdapter:
    """假的 `MCPAdapter`：只记账，不连接。"""

    def __init__(
        self,
        name: str,
        log: list[str],
        *,
        tools: list[BaseTool] | None = None,
        fail_on_enter: bool = False,
        fail_on_list: bool = False,
    ) -> None:
        self.name = name
        self.log = log
        self.tools = tools if tools is not None else [read_file, list_directory]
        self.fail_on_enter = fail_on_enter
        self.fail_on_list = fail_on_list

    async def __aenter__(self) -> FakeAdapter:
        self.log.append(f"{self.name}:enter")
        if self.fail_on_enter:
            raise RuntimeError(f"{self.name} 起不来")
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.log.append(f"{self.name}:exit")

    async def list_tools(self, *, cache_mode: str = "use") -> list[BaseTool]:
        self.log.append(f"{self.name}:list({cache_mode})")
        if self.fail_on_list:
            raise RuntimeError(f"{self.name} 列不出工具")
        return list(self.tools)


def factory(log: list[str], **per_server):
    def make(server: McpServerConfig) -> FakeAdapter:
        name = server.command or server.url or "?"
        return FakeAdapter(name, log, **per_server.get(name, {}))

    return make


def stdio(name: str, **extra) -> McpServerConfig:
    return McpServerConfig(command=name, **extra)


class TestNaming:
    def test_the_prefix_carries_the_server_name(self):
        assert mcp_tool_name("filesystem", "read_file") == "mcp__filesystem__read_file"

    def test_prefixing_keeps_the_rest_of_the_tool_intact(self):
        renamed = prefix_tool(read_file, "fs")
        assert renamed.name == "mcp__fs__read_file"
        assert renamed.description == read_file.description
        assert renamed.args_schema is read_file.args_schema

    def test_prefixing_does_not_touch_the_original(self):
        prefix_tool(read_file, "fs")
        assert read_file.name == "read_file"


class TestOpenMcpTools:
    pytestmark = pytest.mark.asyncio

    async def test_tools_come_out_prefixed(self):
        log: list[str] = []
        async with open_mcp_tools({"fs": stdio("fs")}, adapter_factory=factory(log)) as tools:
            assert sorted(t.name for t in tools) == ["mcp__fs__list_directory", "mcp__fs__read_file"]

    async def test_the_connection_is_opened_and_closed_around_the_body(self):
        log: list[str] = []
        async with open_mcp_tools({"fs": stdio("fs")}, adapter_factory=factory(log)):
            assert log == ["fs:enter", "fs:list(bypass)"]
        assert log == ["fs:enter", "fs:list(bypass)", "fs:exit"]

    async def test_each_server_gets_its_own_adapter(self):
        """决策 #10：不用 `MCPConfig` 一锅端——库不做隔离，隔离是我们自己加的。"""
        log: list[str] = []
        servers = {"a": stdio("a"), "b": stdio("b")}
        async with open_mcp_tools(servers, adapter_factory=factory(log)) as tools:
            assert sorted(t.name for t in tools) == [
                "mcp__a__list_directory",
                "mcp__a__read_file",
                "mcp__b__list_directory",
                "mcp__b__read_file",
            ]
        assert "a:exit" in log and "b:exit" in log

    async def test_a_disabled_server_is_skipped_here_too(self):
        """另一条入口（一次运行）也认这个开关——不然「换条路就连上了禁用的 server」。"""
        log: list[str] = []
        servers = {"off": stdio("off", enabled=False), "on": stdio("on")}

        async with open_mcp_tools(servers, adapter_factory=factory(log)) as tools:
            assert sorted(t.name for t in tools) == ["mcp__on__list_directory", "mcp__on__read_file"]
        assert "off" not in log

    async def test_one_broken_server_does_not_take_down_the_others(self, caplog):
        """决策 #2：跳过 + 响亮警告，不是整体失败。"""
        log: list[str] = []
        servers = {"good": stdio("good"), "bad": stdio("bad"), "also": stdio("also")}
        make = factory(log, bad={"fail_on_enter": True})
        async with open_mcp_tools(servers, adapter_factory=make) as tools:
            assert sorted(t.name for t in tools) == [
                "mcp__also__list_directory",
                "mcp__also__read_file",
                "mcp__good__list_directory",
                "mcp__good__read_file",
            ]
        assert "bad" in caplog.text

    async def test_a_server_that_fails_while_listing_is_also_skipped(self, caplog):
        log: list[str] = []
        make = factory(log, bad={"fail_on_list": True})
        async with open_mcp_tools({"bad": stdio("bad"), "ok": stdio("ok")}, adapter_factory=make) as tools:
            assert sorted(t.name for t in tools) == ["mcp__ok__list_directory", "mcp__ok__read_file"]

    async def test_no_servers_configured_yields_no_tools(self):
        async with open_mcp_tools({}) as tools:
            assert tools == []

    async def test_a_failure_inside_the_body_still_closes_the_connection(self):
        """连接的作用域就是这个 context——body 抛了也必须还回去。"""
        log: list[str] = []
        with pytest.raises(RuntimeError, match="图炸了"):
            async with open_mcp_tools({"fs": stdio("fs")}, adapter_factory=factory(log)):
                raise RuntimeError("图炸了")
        assert log[-1] == "fs:exit"

    async def test_the_connection_is_closed_when_the_consumer_goes_away(self):
        """被动取消的模拟：把消费包在一个 task 里再 cancel 掉。

        `async for` 提前退出**不会**自动关上游生成器，所以生产代码里那三处 `aclosing` 才是必需的。
        这条测的是 `open_mcp_tools` 自己在被 `aclose()` 时会不会关连接。
        """
        log: list[str] = []

        async def consume() -> None:
            async with open_mcp_tools({"fs": stdio("fs")}, adapter_factory=factory(log)) as tools:
                assert tools
                await asyncio.sleep(30)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert log[-1] == "fs:exit"


class TestLoadMcpTools:
    """启动期装载：连一次、拿回工具、**立刻断开**。

    之后每次运行都用这份清单（运行期零 I/O），模型真的调某个工具时工具内部才自己建连。
    """

    pytestmark = pytest.mark.asyncio

    async def test_tools_come_back_prefixed_and_the_connection_is_released(self, caplog):
        """成功的装载记 INFO，失败记 WARNING——所以这条要显式抬到 INFO 才看得见。"""
        caplog.set_level(logging.INFO)
        log: list[str] = []
        tools = await load_mcp_tools({"fs": stdio("fs")}, adapter_factory=factory(log))

        assert sorted(t.name for t in tools) == ["mcp__fs__list_directory", "mcp__fs__read_file"]
        assert log == ["fs:enter", "fs:list(bypass)", "fs:exit"], "拿完工具就该把连接还回去"
        assert "fs" in caplog.text

    async def test_a_broken_server_never_raises(self, caplog):
        """连不上只警告、不拦启动——它只是「这次进程生命周期内没有它的工具」。"""
        log: list[str] = []
        make = factory(log, bad={"fail_on_enter": True})
        tools = await load_mcp_tools({"bad": stdio("bad"), "ok": stdio("ok")}, adapter_factory=make)

        assert [t.name for t in tools] == ["mcp__ok__list_directory", "mcp__ok__read_file"]
        assert "bad" in caplog.text
        assert log == ["bad:enter", "ok:enter", "ok:list(bypass)", "ok:exit"]

    async def test_a_hanging_server_times_out_instead_of_hanging_startup(self, caplog):
        """stdio 没有默认读超时（langchain#40472 OPEN），所以装载自带一个外层的硬超时。"""
        log: list[str] = []

        class Hanging(FakeAdapter):
            async def list_tools(self, *, cache_mode: str = "use") -> list[BaseTool]:
                await asyncio.sleep(30)
                return []

        tools = await load_mcp_tools(
            {"slow": stdio("slow")},
            timeout=0.05,
            adapter_factory=lambda server: Hanging("slow", log),
        )

        assert tools == []
        assert "slow" in caplog.text
        # 超时的警告必须**自己说清是超时**：`asyncio.timeout` 抛的 TimeoutError 的 str() 是空的，
        # 不单独接住的话日志只有一句「TimeoutError」，查的人根本不知道是慢还是配错了。
        assert "超时" in caplog.text

    async def test_a_disabled_server_is_never_connected(self, caplog):
        """关掉 = **根本不建连**，不是「连上再把工具藏起来」。

        这条是那个开关存在的全部理由：启用的 server 要付启动开销（stdio 会 spawn 子进程，
        首次 npx 实测 30.9 秒）。所以断言落在「它从没进过连接那一段」，而不只是「工具里没有它」。
        """
        caplog.set_level(logging.INFO)
        log: list[str] = []

        tools = await load_mcp_tools(
            {"off": stdio("off", enabled=False), "on": stdio("on")},
            adapter_factory=factory(log),
        )

        assert [t.name for t in tools] == ["mcp__on__list_directory", "mcp__on__read_file"]
        assert log == ["on:enter", "on:list(bypass)", "on:exit"], "禁用的那个一次都不该出现"
        assert "已禁用" in caplog.text, "跳过不是静默的——不然「为什么没有它的工具」无从查起"

    async def test_no_servers_means_no_work_at_all(self):
        tools = await load_mcp_tools({})
        assert tools == []
