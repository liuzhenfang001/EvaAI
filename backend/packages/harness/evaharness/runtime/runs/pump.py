"""把内层 langgraph 流泵进队列，并把三种结束各归成一类哨兵。

**为什么要一个显式的 task + 队列**（T5 主动取消）：取消端点要能触达「正在驱动这一轮的那个
协程」，而它藏在生成器的 ``async for`` 里——不搬进 ``asyncio.Task`` 就没有把手。这是 ADR-0006
里 fanout.py 的先例（``create_task`` + sentinel，**不用** ``TaskGroup``，因为消费方提前放弃时
它会抛 ``BaseExceptionGroup``）。

**哨兵是显式值而不是「事后看 task 是否 cancelled」**——后者有竞态：外层读到队列的那一刻，
pump 可能还没真正结束，``task.cancelled()`` 读出来是 False，就会误判成「正常结束」继续往下走。

.. note::
   原住在 ``agents/lead_agent/runner.py``（T19 拆出来，见 ADR-0024）。它与「一次运行怎么跑」
   有关、与「事件长什么样」无关，所以单列一个模块。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

EOF = "eof"
"""内层流正常跑完。"""

CANCELLED = "cancelled"
"""内层流被 ``task.cancel()`` 打断。"""

ERROR = "error"
"""内层流抛了异常（把它当载荷带给外层，而不是在这里 re-raise）。"""


async def pump(stream: AsyncIterator[tuple[str, Any]], queue: asyncio.Queue) -> None:
    """把 *stream* 的每一项推进 *queue*，并以一个哨兵收尾。

    载荷形状统一成 ``(kind, item)``：``("item", <流的一项>)`` 若干，最后一条必定是
    :data:`EOF` / :data:`CANCELLED` / :data:`ERROR` 之一。
    """
    try:
        async for item in stream:
            await queue.put(("item", item))
        await queue.put((EOF, None))
    except asyncio.CancelledError:
        await queue.put((CANCELLED, None))
        raise
    except Exception as exc:
        await queue.put((ERROR, exc))


__all__ = ["CANCELLED", "EOF", "ERROR", "pump"]
