"""Plan-Exec 编排主循环：planner → 派发执行单元 → replanner → 汇总。

选定形态是**纯 async 生成器 + RunEvent 流**（见 ADR-0006），不是 LangGraph 三节点图：
三个角色都是注入点（测试可全用替身）、取消就是 asyncio 取消、事件流天然喂给 SSE。

两处刻意「不做」的事：

- **没有循环上限护栏**。每轮循环必然把一个任务从 ``pending`` 推入终态，进度单调，
  就绪集为空即退出——护栏是永远走不到的死代码。
- **replanner 只在「还有就绪任务」时才被问**。一个条件同时覆盖「计划已跑完」与
  「剩下的都被失败阻塞」，并且让单任务计划（T2 验收场景）零次 replanner 调用。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from uuid import uuid4

from evaharness.agents.plan_exec.events import (
    PlanEvent,
    ReplanEvent,
    RunEvent,
    SummaryEvent,
    TaskEndEvent,
    TaskStartEvent,
)
from evaharness.agents.plan_exec.executor import TaskExecutor, TaskInvocation
from evaharness.agents.plan_exec.planner import Planner
from evaharness.agents.plan_exec.replanner import Replanner
from evaharness.agents.plan_exec.schemas import PlanTask, TaskResult, validate_plan

_TERMINAL_STATUSES = ("done", "failed", "skipped")


def _ready_tasks(tasks: Sequence[PlanTask], status: dict[str, str]) -> list[PlanTask]:
    """就绪 = 自身未开跑 且 依赖全部成功。

    依赖「成功」而非「已终结」：上游失败时下游没有可用的输入，跳过比硬跑合理。
    悬空依赖永远不满足，于是该任务自然沉淀为 ``skipped``。
    """
    return [
        task
        for task in tasks
        if status.get(task.id) == "pending" and all(status.get(dep) == "done" for dep in task.depends_on)
    ]


def _summary_status(stopped: bool, results: Sequence[TaskResult]) -> str:
    if stopped:
        return "stopped"
    return "completed" if all(result.status == "done" for result in results) else "partial"


async def run_plan_exec(
    request: str,
    *,
    planner: Planner,
    executor: TaskExecutor,
    replanner: Replanner | None = None,
) -> AsyncIterator[RunEvent]:
    """跑完一次 Plan-Exec，产出完整的事件流。

    Args:
        request: 用户的原始请求。
        planner: 产出结构化计划的角色。
        executor: 单个任务的执行单元（默认实现是 ReAct）。
        replanner: 每批任务后判定「继续还是结束」的角色；``None`` 表示从不叫停。

    Yields:
        ``PlanEvent`` → 逐个任务的 ``TaskStartEvent`` / ``MessagesEvent`` / ``TaskEndEvent``
        → ``SummaryEvent``。

    Raises:
        PlanValidationError: 计划结构性非法（此时 ``PlanEvent`` 已产出，前端仍能看到坏计划）。
    """
    run_id = uuid4().hex[:12]
    plan = await planner(request)
    yield PlanEvent(tasks=plan.tasks)
    validate_plan(plan)

    status: dict[str, str] = {task.id: "pending" for task in plan.tasks}
    results: dict[str, TaskResult] = {}
    stopped = False

    while True:
        ready = _ready_tasks(plan.tasks, status)
        if not ready:
            break
        task = ready[0]
        status[task.id] = "running"
        yield TaskStartEvent(task_id=task.id, description=task.description, depends_on=task.depends_on)

        result: TaskResult | None = None
        invocation = TaskInvocation(
            task=task,
            dependency_results=tuple(results[dep] for dep in task.depends_on if dep in results),
            run_id=run_id,
        )
        async for event in executor(invocation):
            if isinstance(event, TaskEndEvent):
                result = TaskResult(
                    task_id=event.task_id,
                    status=event.status,
                    output=event.output,
                    error=event.error,
                )
            yield event

        if result is None:
            # 执行单元中途没了（既没发终态也没抛）：补一条，不能让任务静默消失。
            result = TaskResult(task_id=task.id, status="failed", error="执行单元未返回终态事件")
            yield TaskEndEvent(task_id=task.id, status="failed", error=result.error)

        status[task.id] = result.status
        results[task.id] = result

        if replanner is not None and _ready_tasks(plan.tasks, status):
            decision = await replanner(
                request,
                plan,
                [results[t.id] for t in plan.tasks if t.id in results],
            )
            yield ReplanEvent(should_continue=decision.should_continue, reason=decision.reason)
            if not decision.should_continue:
                stopped = True
                break

    for task in plan.tasks:
        if status[task.id] not in _TERMINAL_STATUSES:
            status[task.id] = "skipped"
            results[task.id] = TaskResult(task_id=task.id, status="skipped")
            yield TaskEndEvent(task_id=task.id, status="skipped")

    ordered = [results[task.id] for task in plan.tasks]
    yield SummaryEvent(
        status=_summary_status(stopped, ordered),
        results=ordered,
        failed=[result.task_id for result in ordered if result.status == "failed"],
        skipped=[result.task_id for result in ordered if result.status == "skipped"],
    )


__all__ = ["run_plan_exec"]
