"""「跑一次」的执行：入口、并发编排、取消、终态读取。

- :mod:`~evaharness.runtime.runs.run` —— :func:`run_agent`（唯一入口）；
- :mod:`~evaharness.runtime.runs.pump` —— 内层流 → 队列，三种结束各归一类哨兵；
- :mod:`~evaharness.runtime.runs.cancellation` —— :class:`RunCancellation` + :class:`RunRegistry`；
- :mod:`~evaharness.runtime.runs.state` —— :func:`final_text` 与 :func:`pending_interrupt`。

**它依赖契约层（``runtime.event`` / ``runtime.context``）与 ``checkpoint``（取坐标），
不依赖 ``agents``。**
"""

from __future__ import annotations

from evaharness.runtime.runs.cancellation import RunCancellation, RunRegistry
from evaharness.runtime.runs.pump import CANCELLED, EOF, ERROR, pump
from evaharness.runtime.runs.run import run_agent
from evaharness.runtime.runs.state import final_text, pending_interrupt

__all__ = [
    "CANCELLED",
    "EOF",
    "ERROR",
    "RunCancellation",
    "RunRegistry",
    "final_text",
    "pending_interrupt",
    "pump",
    "run_agent",
]
