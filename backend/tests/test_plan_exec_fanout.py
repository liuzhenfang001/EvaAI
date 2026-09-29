"""契约测试：并行批次的事件合流。

这一层的关键不是「能并发」，而是**四种退出路径都干净**——尤其是消费方提前放弃
（SSE 客户端断连走的正是这条路径）与取消。

用 ``asyncio.TaskGroup`` 实现会在放弃路径抛 ``BaseExceptionGroup``：这不是理论
风险，是实测结果，也是这里逐条钉死退出路径的原因（见 ADR-0008）。

**本文件一律不用 sleep 制造时序。** 本机（Windows ``ProactorEventLoop``）的定时器
基于 ``time.monotonic()``，而该时钟在 Windows 上以约 15.6ms 为粒度跳变：实测
``await asyncio.sleep(0.01)`` 只要 0.16ms 就返回（``sleep(0.2)`` 则准确）。靠毫秒级
sleep 拉开先后的测试在这里是随机失败的。交错顺序一律用**接力事件**构造出来，
「挂住」用**永不 set 的 Event** 表达。
"""

import asyncio
from collections.abc import AsyncIterator

import pytest

from evaharness.agents.plan_exec.events import MessagesEvent
from evaharness.agents.plan_exec.fanout import StreamCrashed, merge_named_streams


def _msg(task_id: str, text: str) -> MessagesEvent:
    return MessagesEvent(task_id=task_id, chunk={"content": text, "type": "AIMessageChunk"})


def _go(steps: int) -> list[asyncio.Event]:
    """``steps+1`` 个接力事件；第 0 步放行，其余等前一步产出后放行。"""
    events = [asyncio.Event() for _ in range(steps + 1)]
    events[0].set()
    return events


async def _relay(
    name: str,
    mine: list[tuple[int, str]],
    go: list[asyncio.Event],
    *,
    raises: BaseException | None = None,
    hang: bool = False,
) -> AsyncIterator[MessagesEvent]:
    """按全局步号产出的子流：等轮到自己才产出，产出后放行下一步。

    ``mine`` 是 ``(全局步号, 文本)``，同一个 ``go`` 由所有子流共享——于是
    「谁先谁后」是写死的构造事实，不是调度运气。
    """
    for step, text in mine:
        await go[step].wait()
        yield _msg(name, text)
        go[step + 1].set()
    if raises is not None:
        raise raises
    if hang:
        await asyncio.Event().wait()


def _lanes(plan: list[tuple[int, str, str]]) -> list[tuple[str, AsyncIterator[MessagesEvent]]]:
    """把 ``[(全局步号, 流名, 文本)]`` 编成各流的接力脚本。"""
    go = _go(len(plan))
    grouped: dict[str, list[tuple[int, str]]] = {}
    for step, name, text in plan:
        grouped.setdefault(name, []).append((step, text))
    return [(name, _relay(name, mine, go)) for name, mine in grouped.items()]


async def _forever(name: str, first: str | None, cancelled: list[str]) -> AsyncIterator[MessagesEvent]:
    """先给一个事件（可选），然后永远挂起——用来断言「取消真的穿透进来了」。"""
    gate = asyncio.Event()
    try:
        if first is not None:
            yield _msg(name, first)
        await gate.wait()
    except asyncio.CancelledError:
        cancelled.append(name)
        raise


async def _collect(stream) -> list:
    return [item async for item in stream]


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_events_are_interleaved_in_arrival_order():
    """两个流的事件按「谁先产出谁先出」交错，而不是一个流跑完再跑另一个。"""
    lanes = _lanes(
        [
            (0, "a", "a0"),
            (1, "b", "b0"),
            (2, "a", "a1"),
            (3, "b", "b1"),
            (4, "a", "a2"),
            (5, "b", "b2"),
        ]
    )

    items = await _collect(merge_named_streams(lanes))

    assert [item.chunk["content"] for item in items] == ["a0", "b0", "a1", "b1", "a2", "b2"]


@pytest.mark.asyncio
async def test_empty_input_yields_nothing():
    assert await _collect(merge_named_streams([])) == []


@pytest.mark.asyncio
async def test_single_stream_is_passed_through():
    items = await _collect(merge_named_streams(_lanes([(0, "a", "只有我")])))

    assert [item.chunk["content"] for item in items] == ["只有我"]


# --------------------------------------------------------------------------- #
# 退出路径（这一组才是本模块存在的理由）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_consumer_abandonment_closes_without_raising():
    """消费方跑到一半放弃（客户端断连）：``aclose()`` 必须干净。

    TaskGroup 实现会在这里抛 ``BaseExceptionGroup``——SSE 层会把它当成未处理
    异常，所以这条是硬约束而非洁癖。
    """
    cancelled: list[str] = []
    stream = merge_named_streams(
        [
            ("a", _forever("a", "a0", cancelled)),
            ("b", _forever("b", "b0", cancelled)),
        ]
    )

    got = []
    async for item in stream:
        got.append(item)
        if len(got) == 2:
            break
    await stream.aclose()  # 不得抛异常

    assert sorted(cancelled) == ["a", "b"], "放弃后子流必须被取消，不能让它们继续挂着"


@pytest.mark.asyncio
async def test_cancellation_propagates_to_child_streams():
    """硬取消（ADR-0005）必须穿透到子流内部正在 await 的地方。"""
    cancelled: list[str] = []
    stream = merge_named_streams(
        [
            ("a", _forever("a", None, cancelled)),
            ("b", _forever("b", None, cancelled)),
        ]
    )

    async def consume():
        async for _ in stream:
            pass

    task = asyncio.create_task(consume())
    # 只让出控制权，不赌时间：两次 sleep(0) 足以让 consume 与两个子泵跑到 gate.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert sorted(cancelled) == ["a", "b"]


@pytest.mark.asyncio
async def test_a_crashed_stream_reports_itself_and_lets_siblings_finish():
    """某个流自己抛了异常：作为数据交回调用方，同批其他流照常跑完（best-effort）。"""
    go = _go(3)
    stream = merge_named_streams(
        [
            ("a", _relay("a", [(0, "a0")], go, raises=RuntimeError("执行单元不合约定地抛了"))),
            ("b", _relay("b", [(1, "b0"), (2, "b1")], go)),
        ]
    )

    items = await _collect(stream)

    crashed = [item for item in items if isinstance(item, StreamCrashed)]
    assert len(crashed) == 1
    assert crashed[0].name == "a"
    assert isinstance(crashed[0].error, RuntimeError)
    assert str(crashed[0].error) == "执行单元不合约定地抛了"
    assert [item.chunk["content"] for item in items if isinstance(item, MessagesEvent)] == ["a0", "b0", "b1"]


@pytest.mark.asyncio
async def test_a_hanging_stream_does_not_block_siblings():
    """一个流挂住不影响其他流的产出——合流不能是「等最慢的」。"""
    go = _go(2)
    stream = merge_named_streams(
        [
            ("slow", _relay("slow", [(0, "慢的先给一个")], go, hang=True)),
            ("fast", _relay("fast", [(1, "快")], go)),
        ]
    )

    got = []
    async for item in stream:
        got.append(item.chunk["content"])
        if len(got) == 2:
            break
    await stream.aclose()

    assert got == ["慢的先给一个", "快"]
