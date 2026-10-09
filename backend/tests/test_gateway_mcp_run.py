"""MCP 连接在**一次运行**里的生死：四条路径都要把它还回去。

这一片最容易错、也最难在代码评审里看出来的一件事是：`async for` 提前退出**不会**关上游生成器。
所以「连接关掉了」必须是被测出来的，而不是被推理出来的——被动断连（客户端走了）那条尤其如此，
它走的是 `CancelledError` 从最外层穿透解开的路径，中间任何一层少了 `aclosing` 都会漏掉一个
npx 子进程，而**不会**有任何测试变红。

所以四条路径各一条测试：跑完 / 图内抛 / 消费者走开 / 任务被取消。再加一条走**完整链路**
（``sse_consumer(journaled(_run_lead(...)))``）的，因为线上真正被丢弃的是最外层那个生成器。
"""

from __future__ import annotations

import asyncio
import itertools
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver
from plan_exec_fakes import ToolCapableFake, fake_app_config

from app.gateway import mcp as mcp_module
from app.gateway.agents import AgentFactory
from app.gateway.routers import chat
from app.gateway.sse import sse_consumer
from evaharness.agents.lead_agent import RunCancellation
from evaharness.agents.options import RunOptions
from evaharness.config.extensions_config import ExtensionsConfig
from evaharness.journal import RunJournal, journaled
from evaharness.journal.stores import MemoryJournalStore

THREAD = "th-1"


@tool("read_file")
def _mcp_read(path: str) -> str:
    """一个 MCP 工具，名字是**服务端给的原名**——前缀由装配层加。"""
    return path


class FakeAdapter:
    """假的 `MCPAdapter`：只记账。真连接由 `test_mcp_client.py` 那几条契约测试负责。"""

    def __init__(self, log: list[str], *, fail: bool = False, list_delay: float = 0.0) -> None:
        self.log = log
        self.fail = fail
        self.list_delay = list_delay

    async def __aenter__(self) -> FakeAdapter:
        self.log.append("enter")
        if self.fail:
            raise RuntimeError("这个 server 起不来")
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.log.append("exit")

    async def list_tools(self, *, cache_mode: str = "use") -> list[BaseTool]:
        self.log.append("list")
        if self.list_delay:
            await asyncio.sleep(self.list_delay)
        return [_mcp_read]


class RecordingFactory(AgentFactory):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.extra_tools: list[list] = []

    def lead(self, options=None, *, extra_tools=()):  # noqa: ANN001, ANN201
        self.extra_tools.append(list(extra_tools))
        return super().lead(options, extra_tools=extra_tools)


def _state(monkeypatch, log: list[str], *, fail: bool = False, list_delay: float = 0.0, servers=None):
    """一个只够 `_run_lead` 用的假 app.state。

    `mcp_tools_for_run` 的 `adapter_factory` 缺省是**定义时绑定**的，monkeypatch 模块属性改不动
    它——所以这里用一层薄包装把假 adapter 传进**真的** `mcp_tools_for_run`。这样被绕过的只有传输
    层，改名、逐 server 隔离、与业务工具合并那几段都还是产品代码。
    """
    factory = RecordingFactory(
        fake_app_config(),
        [],
        checkpointer=InMemorySaver(),
        model_factory=lambda _name, _options=None: ToolCapableFake(
            messages=itertools.cycle([AIMessage(content="好的。")])
        ),
    )
    state = SimpleNamespace(
        agent_factory=factory,
        extensions_config=ExtensionsConfig.model_validate(
            {"mcpServers": servers if servers is not None else {"fs": {"command": "npx.cmd"}}}
        ),
    )
    real = mcp_module.mcp_tools_for_run

    def patched(_state):  # noqa: ANN001
        return real(
            _state,
            adapter_factory=lambda _server: FakeAdapter(log, fail=fail, list_delay=list_delay),
        )

    monkeypatch.setattr(chat, "mcp_tools_for_run", patched)
    return state


def _run(state, content: str = "读个文件"):
    return chat._run_lead(
        state,
        chat.ChatRequest(content=content, thread_id=THREAD),
        RunOptions(thinking="high"),
        "run-1",
        THREAD,
        # 取消接缝在这里用不着：这一层只关心连接与图的生死。
        RunCancellation(),
    )


async def _drain(state) -> list:
    return [event async for event in _run(state)]


class TestTheToolsReachTheGraph:
    pytestmark = pytest.mark.asyncio

    async def test_mcp_tools_are_handed_to_the_graph(self, monkeypatch):
        log: list[str] = []
        state = _state(monkeypatch, log)

        [event async for event in _run(state)]

        assert [t.name for t in state.agent_factory.extra_tools[0]] == ["mcp__fs__read_file"]

    async def test_no_servers_configured_means_no_connection_attempted(self, monkeypatch):
        """没配 MCP 的运行路径必须与 T12 之前**一字不差**——不建连接、不碰库。"""
        log: list[str] = []
        state = _state(monkeypatch, log, servers={})

        [event async for event in _run(state)]

        assert log == []
        assert state.agent_factory.extra_tools == [[]]


class TestTheConnectionIsAlwaysReturned:
    pytestmark = pytest.mark.asyncio

    async def test_after_a_normal_run(self, monkeypatch):
        log: list[str] = []
        state = _state(monkeypatch, log)

        [event async for event in _run(state)]

        assert log[-1] == "exit"

    async def test_when_the_consumer_walks_away(self, monkeypatch):
        """被动断连：消费了一帧就走人，连接必须还回去。

        这就是 `aclosing` 存在的理由——`async for` 提前退出不关上游，少了它这里会漏一个子进程。
        """
        log: list[str] = []
        state = _state(monkeypatch, log)

        stream = _run(state)
        await anext(stream)
        await stream.aclose()

        assert log[0] == "enter"
        assert log[-1] == "exit", "消费者走开时连接没关——多半是 `aclosing` 没了"

    async def test_when_the_task_is_cancelled_mid_run(self, monkeypatch):
        """硬取消：`CancelledError` 从生成器**体内**的 await 上抛出来，连接仍要还。

        这是线上真实的那条路——客户端断开时，取消打在 `queue.get()` 之类的内部 await 上
        （ADR-0011 记的就是这个），于是异常**穿过** `async with`，`__aexit__` 被 await。
        这里用「列工具列到一半」来制造同一个窗口。
        """
        log: list[str] = []
        state = _state(monkeypatch, log, list_delay=30)

        task = asyncio.create_task(_drain(state))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert log == ["enter", "list", "exit"], "取消时连接没关"

    async def test_abandoning_the_generator_without_closing_it(self, monkeypatch):
        """**已知边界**：消费者直接 `break` 走人、既不 `aclose` 也不抛，连接不会当场关。

        `async for` 语义如此——弃置的异步生成器要等 GC 时的 finalizer 才收尾，那是
        **不确定**的。所以线上每一层都必须套 `aclosing`（`sse_consumer` / `journaled` /
        `_run_lead` 三处都套了），不能指望这一层自己兜住。
        这条测试把这个边界写明：它绿不代表安全，只代表我们知道它长什么样。
        """
        log: list[str] = []
        state = _state(monkeypatch, log)

        async for _ in _run(state):
            break

        assert log[-1] != "exit"

    async def test_through_the_whole_sse_and_journal_chain(self, monkeypatch):
        """线上真正被丢弃的是最外层那个生成器，所以整条链一起测。

        `sse_consumer` → `journaled` → `_run_lead` → `AsyncExitStack`：中间任何一层少了
        `aclosing`，断连时连接就漏了，而且不会有别的测试发现。
        """
        log: list[str] = []
        state = _state(monkeypatch, log)
        journal = RunJournal(MemoryJournalStore(), THREAD, flush_threshold=20)

        frames = sse_consumer(journaled(_run(state), journal))
        await anext(frames)
        await frames.aclose()

        assert log[-1] == "exit", "SSE 层被关掉时，最里面的 MCP 连接没跟着关"


class TestDegrade:
    pytestmark = pytest.mark.asyncio

    async def test_a_broken_server_does_not_fail_the_run(self, monkeypatch, caplog):
        """决策 #2 / #12：server 起不来 = 这次运行没有它的工具，不是这次运行失败。"""
        log: list[str] = []
        state = _state(monkeypatch, log, fail=True)

        events = [event async for event in _run(state)]

        assert log == ["enter"], "起不来就不该继续走到 list_tools 或 exit"
        assert state.agent_factory.extra_tools == [[]], "工具表照常建，只是没有 MCP 那批"
        assert [type(e).__name__ for e in events][-1] == "RunEndEvent"
        assert "fs" in caplog.text, "跳过必须是响亮的，不是静默的"
