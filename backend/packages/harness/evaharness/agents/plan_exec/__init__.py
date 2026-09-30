"""Plan-Exec 编排：planner → ReAct 执行单元 → replanner → 汇总。

与 ReAct 是嵌套而非二选一的关系（ADR-0004）：这一层负责任务级规划与派发，
单个任务内部的推理-行动循环交给 :mod:`evaharness.agents.react`。
"""

from evaharness.agents.plan_exec.events import (
    EventCategory,
    MessagesEvent,
    PlanEvent,
    ReplanEvent,
    RunEvent,
    RunEventAdapter,
    RunStartEvent,
    SummaryEvent,
    TaskEndEvent,
    TaskStartEvent,
)
from evaharness.agents.plan_exec.executor import (
    TaskExecutor,
    TaskInvocation,
    final_text,
    make_react_executor,
    render_task_prompt,
)
from evaharness.agents.plan_exec.fanout import StreamCrashed, merge_named_streams
from evaharness.agents.plan_exec.orchestrator import run_plan_exec
from evaharness.agents.plan_exec.planner import Planner, make_planner
from evaharness.agents.plan_exec.replanner import Replanner, make_replanner, render_replan_context
from evaharness.agents.plan_exec.schemas import (
    Plan,
    PlanTask,
    PlanValidationError,
    ReplanDecision,
    TaskResult,
    TerminalTaskStatus,
    validate_plan,
)

__all__ = [
    "MessagesEvent",
    "Plan",
    "PlanEvent",
    "PlanTask",
    "ReplanEvent",
    "PlanValidationError",
    "Planner",
    "ReplanDecision",
    "Replanner",
    "EventCategory",
    "RunEvent",
    "RunEventAdapter",
    "RunStartEvent",
    "StreamCrashed",
    "SummaryEvent",
    "TaskEndEvent",
    "TaskExecutor",
    "TaskInvocation",
    "TaskResult",
    "TaskStartEvent",
    "TerminalTaskStatus",
    "final_text",
    "make_planner",
    "make_react_executor",
    "make_replanner",
    "merge_named_streams",
    "render_replan_context",
    "render_task_prompt",
    "run_plan_exec",
    "validate_plan",
]
