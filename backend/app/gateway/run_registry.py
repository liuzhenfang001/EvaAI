"""运行中 run 的登记表：``run_id -> 正在驱动该 run 的内层 asyncio.Task``。

取消端点靠它按 ``run_id`` 触达一个正在跑的 run，触发主动硬取消（T5）。registry 只认
「由 :class:`~evaharness.agents.lead_agent.runner.RunCancellation` 交进来的 pump task」，
不知道图、不知道流——harness 与 app 的边界仍然只靠那个小协议。

单进程、单事件循环里，dict 的读写本身是原子的（两次事件循环调度之间不会被打断），
所以**不需要锁**；跨 worker 的取消不是本片的范围（本地个人助手单 worker 跑）。
"""

from __future__ import annotations

import asyncio


class RunRegistry:
    """``run_id`` 到内层执行 task 的登记表。"""

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


__all__ = ["RunRegistry"]
