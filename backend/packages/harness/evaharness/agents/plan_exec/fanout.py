"""并行批次的事件合流。

T3 把「按依赖顺序逐个执行」换成「一批并发派发」，于是需要把多个执行单元的事件流
合成一路——编排器的输出契约仍是**单条** RunEvent 流（ADR-0006），并发只是产生方式。

**为什么不用 ``asyncio.TaskGroup``**：消费方提前放弃时（SSE 客户端断连会走到
``aclose()``），TaskGroup 的 ``__aexit__`` 会把子任务的取消包成 ``BaseExceptionGroup``
抛出来，SSE 层会把它当作未处理异常。显式 ``create_task`` + sentinel 在四种退出
路径下都干净：跑完 / 放弃（不抛）/ 取消（``CancelledError`` 原样穿透）/ 子流崩溃
（作为数据交回，同批其余照跑）。这一条是实测结论，不是推断，见 ADR-0008。

sentinel 放在子泵的 ``finally`` 里，所以「子流抛异常」与「子流正常耗尽」都会让
计数递减，主循环不会漏等某个流。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from evaharness.agents.plan_exec.events import RunEvent

_SENTINEL = None


@dataclass(frozen=True, slots=True)
class StreamCrashed:
    """某个事件流自身抛了异常。

    执行单元的约定是「失败用 ``TaskEndEvent``，不抛」；真抛了就是实现不合约定。
    这里只把它作为数据交回编排器，不在这里替它决定怎么记账。
    """

    name: str
    error: BaseException


async def merge_named_streams(
    streams: Sequence[tuple[str, AsyncIterator[RunEvent]]],
) -> AsyncIterator[RunEvent | StreamCrashed]:
    """并发消费多个事件流，按到达序交错产出。

    Args:
        streams: ``(名字, 事件流)`` 序列；名字只用于 :class:`StreamCrashed` 归属。

    Yields:
        各子流产出的事件（谁先产出谁先出）；某个子流崩溃时产出一条 ``StreamCrashed``。
    """
    queue: asyncio.Queue[RunEvent | StreamCrashed | None] = asyncio.Queue()

    async def pump(source: AsyncIterator[RunEvent], name: str) -> None:
        try:
            async for event in source:
                await queue.put(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await queue.put(StreamCrashed(name=name, error=exc))
        finally:
            await queue.put(_SENTINEL)

    pumps = [asyncio.create_task(pump(source, name)) for name, source in streams]
    try:
        pending = len(pumps)
        while pending:
            item = await queue.get()
            if item is _SENTINEL:
                pending -= 1
                continue
            yield item
    finally:
        # 取消路径下这个 await 仍能完成（实测），所以可以老老实实等子流收尾。
        # 不做「不 await 的脏收尾」：那样「取消是否穿透到子流」就不可断言了。
        for task in pumps:
            task.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)


__all__ = ["StreamCrashed", "merge_named_streams"]
