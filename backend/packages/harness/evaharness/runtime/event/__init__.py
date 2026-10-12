"""事件相关逻辑：契约、工具载荷、以及「图流 → 事件」的翻译。

- :mod:`~evaharness.runtime.event.contract` —— :class:`RunEvent` 判别联合（含 Plan-Exec 的
  **只读**遗留契约，老 journal 回放要用）；
- :mod:`~evaharness.runtime.event.custom` —— 工具经 custom 流送载荷时两边共用的**契约**
  （生产者在 app，消费者在本包）；
- :mod:`~evaharness.runtime.event.translate` —— 把 ``astream`` 的三种载荷形状翻成事件。

**这一层不 import 任何 L2 机制包，也不 import ``agents``**——它是 L1 契约层，只依赖
:mod:`evaharness.runtime.context` 的 ``ThinkingLevel``。
"""

from __future__ import annotations

from evaharness.runtime.event.contract import (
    DEFAULT_LANE,
    EventCategory,
    FilesPresentedEvent,
    MessagesEvent,
    PlanEvent,
    PlanTask,
    PresentedFile,
    ReplanEvent,
    RunEndEvent,
    RunEvent,
    RunEventAdapter,
    RunStartEvent,
    SummaryEvent,
    TaskEndEvent,
    TaskResult,
    TaskStartEvent,
    TerminalTaskStatus,
)
from evaharness.runtime.event.custom import PRESENT_FILES_KIND, present_files_payload
from evaharness.runtime.event.translate import produces_event, translate

__all__ = [
    "DEFAULT_LANE",
    "PRESENT_FILES_KIND",
    "EventCategory",
    "FilesPresentedEvent",
    "MessagesEvent",
    "PlanEvent",
    "PlanTask",
    "PresentedFile",
    "ReplanEvent",
    "RunEndEvent",
    "RunEvent",
    "RunEventAdapter",
    "RunStartEvent",
    "SummaryEvent",
    "TaskEndEvent",
    "TaskResult",
    "TaskStartEvent",
    "TerminalTaskStatus",
    "present_files_payload",
    "produces_event",
    "translate",
]
