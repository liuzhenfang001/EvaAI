"""主动取消这条协议的两半：**接缝**（一次运行交出它的内层 task）与**登记表**（按 run 触达它）。

分两层是有意的：机制（一次运行怎么把把手交出去）没有部署假设，而「登记表活在哪个进程、
要不要跨 worker」是部署的事。所以接缝在 :class:`RunCancellation`，默认实现是 :class:`RunRegistry`
（单进程、单事件循环）——将来若真要跨 worker，换掉的是登记表那一半，运行入口不动。

.. note::
   原先是两半分居两层：``RunCancellation`` 在 ``agents/lead_agent/runner.py``，``RunRegistry``
   在 ``app/gateway/run_registry.py``，中间靠网关层的 lambda 缝合（T19 合到一处，见 ADR-0024）。
   ``RunRegistry`` 是纯 ``asyncio`` 的 dict、零业务知识，本来就不该住在业务层。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable


class RunCancellation:
    """主动取消的接缝：运行入口把驱动内层流的 task 交进来，由调用方接进自己的登记表。

    这里只声明两个钩子、**不 import 上层**——harness 不知道网关长什么样，也不该知道
    （守住 ``tests/test_harness_boundary.py``）。缺省 no-op，让「不取消」的调用方（测试、
    纯推理的运行）不必传。
    """

    def __init__(
        self,
        register: Callable[[asyncio.Task], None] | None = None,
        unregister: Callable[[], None] | None = None,
    ) -> None:
        self._register = register or (lambda _: None)
        self._unregister = unregister or (lambda: None)

    def register(self, task: asyncio.Task) -> None:
        """把驱动本轮的内层 task 交出去（调用方据此按 run_id 触达它）。"""
        self._register(task)

    def unregister(self) -> None:
        """运行结束（无论何种终态）时摘除登记，避免 registry 泄漏。"""
        self._unregister()


class RunRegistry:
    """``run_id`` 到内层执行 task 的登记表（缺省实现）。

    单进程、单事件循环里，dict 的读写本身是原子的（两次事件循环调度之间不会被打断），
    所以**不需要锁**；跨 worker 的取消不在本项目范围内（本地个人助手单 worker 跑）。
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}

    def register(self, run_id: str, task: asyncio.Task) -> None:
        """登记一个正在跑的 run；同名 run_id 后者覆盖前者（run_id 足够随机，不现实）。"""
        self._tasks[run_id] = task

    def unregister(self, run_id: str) -> None:
        """摘除登记。运行结束（无论何种终态）都会调，避免 registry 无界增长。"""
        self._tasks.pop(run_id, None)

    def cancel(self, run_id: str) -> bool:
        """取消 ``run_id`` 对应的运行；命中且仍在跑返回 ``True``，否则 ``False``。

        ``task.done()`` 的判据是必要的：pump 可能刚跑完、还没来得及 ``unregister``，
        对已结束的 task 再 ``cancel()`` 是 no-op，但「取消成功了」这个返回值会撒谎。
        """
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True


__all__ = ["RunCancellation", "RunRegistry"]
