"""运行层：一次 agent 运行的事件契约、运行上下文，以及跑它的那一套机制。

**这一层是 harness 的契约中枢**——三个子包各自回答一个问题：

- :mod:`~evaharness.runtime.event` —— 一次运行**说什么**（RunEvent 契约、工具载荷、图流翻译）；
- :mod:`~evaharness.runtime.context` —— 这一轮**是谁**（RunContext、三个访问器、运行参数）；
- :mod:`~evaharness.runtime.runs` —— 怎么**跑起来**（入口、并发编排、取消、终态）。

它**不 import ``agents``、不 import 任何工具包**，所以「写一个工具」与「记一份 journal」
都不必认识具体是哪个 agent 在跑（分层见 ADR-0024）。

**只从本模块 import**：子包路径是给读代码的人导航用的，不是 API 面。照子包路径 import 等于
把内部结构焊死——将来 ``event`` 拆成两个子包时，所有调用方都得跟着改。参照项目 DeerFlow 的
``runtime/__init__.py`` 也是同一手法（它的注释明写只 re-export 公开面）。

.. note::
   ``evaharness.runtime`` 与 ``langgraph.runtime`` 同名不同物：后者是上游交给中间件/工具的
   运行期对象。绝对 import 下两者不冲突，但读到 ``Runtime`` 时先看清它是从哪来的。
"""

from __future__ import annotations

from evaharness.runtime.context import (
    FALLBACK_BUCKET,
    SAFE_SESSION_ID,
    RunContext,
    RunOptions,
    ThinkingLevel,
    sandbox_layout,
    session_id,
    thread_bucket,
)
from evaharness.runtime.event import (
    DEFAULT_LANE,
    PRESENT_FILES_KIND,
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
    present_files_payload,
)
from evaharness.runtime.runs import (
    RunCancellation,
    RunRegistry,
    final_text,
    run_agent,
)

__all__ = [
    "DEFAULT_LANE",
    "FALLBACK_BUCKET",
    "PRESENT_FILES_KIND",
    "SAFE_SESSION_ID",
    "EventCategory",
    "FilesPresentedEvent",
    "MessagesEvent",
    "PlanEvent",
    "PlanTask",
    "PresentedFile",
    "ReplanEvent",
    "RunCancellation",
    "RunContext",
    "RunEndEvent",
    "RunEvent",
    "RunEventAdapter",
    "RunOptions",
    "RunRegistry",
    "RunStartEvent",
    "SummaryEvent",
    "TaskEndEvent",
    "TaskResult",
    "TaskStartEvent",
    "TerminalTaskStatus",
    "ThinkingLevel",
    "final_text",
    "present_files_payload",
    "run_agent",
    "sandbox_layout",
    "session_id",
    "thread_bucket",
]
