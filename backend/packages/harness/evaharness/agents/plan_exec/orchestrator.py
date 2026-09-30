"""Plan-Exec 编排主循环：planner → 派发执行单元 → replanner → 汇总。

选定形态是**纯 async 生成器 + RunEvent 流**（见 ADR-0006），不是 LangGraph 三节点图：
三个角色都是注入点（测试可全用替身）、取消就是 asyncio 取消、事件流天然喂给 SSE
与 RunJournal。

T3 起派发是**按批并发**的：一次把所有就绪任务一起发出去（依赖关系决定谁在哪一批），
事件经由 :func:`~evaharness.agents.plan_exec.fanout.merge_named_streams` 合流回
单条 RunEvent 流。计划规模小（个人助手），不做并发上限。

两处刻意「不做」的事：

- **没有循环上限护栏**。每轮循环必然把至少一个任务从 ``pending`` 推入终态，进度
  单调，就绪集为空即退出——护栏是永远走不到的死代码。
- **replanner 只在「还有就绪任务」时才被问**。一个条件同时覆盖「计划已跑完」与
  「剩下的都被失败阻塞」，并且让单任务计划（T2 验收场景）零次 replanner 调用。
  注意 T3 之后它的咨询时机是**批与批之间**：一批独立任务跑完、且下游还有活时才会
  被问——这是「每批任务结束后判定」在并行下的正确推论，不是能力缩水。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from uuid import uuid4

from evaharness.agents.plan_exec.events import (
    PlanEvent,
    ReplanEvent,
    RunEvent,
    RunStartEvent,
    SummaryEvent,
    TaskEndEvent,
    TaskStartEvent,
)
from evaharness.agents.plan_exec.executor import TaskExecutor, TaskInvocation
from evaharness.agents.plan_exec.fanout import StreamCrashed, merge_named_streams
from evaharness.agents.plan_exec.planner import Planner
from evaharness.agents.plan_exec.replanner import Replanner
from evaharness.agents.plan_exec.schemas import PlanTask, TaskResult, validate_plan

_TERMINAL_STATUSES = ("done", "failed", "skipped")


def _ready_tasks(tasks: Sequence[PlanTask], status: dict[str, str]) -> list[PlanTask]:
    """就绪 = 自身未开跑 且 依赖全部成功。

    依赖「成功」而非「已终结」：上游失败时下游没有可用的输入，跳过比硬跑合理。
    悬空依赖永远不满足，于是该任务自然沉淀为 ``skipped``。

    返回**全部**就绪任务而非第一个——它们之间没有依赖关系，可以一起跑。
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


def _missing_terminal_error(error: BaseException | None) -> str:
    """任务没拿到终态事件时的说明文字。"""
    if error is None:
        return "执行单元未返回终态事件"
    return f"执行单元抛出未捕获异常：{type(error).__name__}: {error}"


async def run_plan_exec(
    request: str,
    *,
    planner: Planner,
    executor: TaskExecutor,
    replanner: Replanner | None = None,
    run_id: str | None = None,
    thread_id: str | None = None,
) -> AsyncIterator[RunEvent]:
    """跑完一次 Plan-Exec，产出完整的事件流。

    Args:
        request: 用户的原始请求。
        planner: 产出结构化计划的角色。
        executor: 单个任务的执行单元（默认实现是 ReAct）。
        replanner: 每批任务后判定「继续还是结束」的角色；``None`` 表示从不叫停。
        run_id: 本次运行的标识；缺省自生。调用方指定它是为了让 RunJournal 与前端
            用同一个 id 定位这次运行。
        thread_id: 本次运行所属的**会话**标识；缺省自生。语义与参照项目一致：
            **给了就用，没给就生成**——所以前端可以先本地生成再传进来，也可以
            完全不管、从 ``RunStartEvent`` 里学到服务端生成的那个。

    Yields:
        ``RunStartEvent`` → ``PlanEvent`` → 逐批的 ``TaskStartEvent`` /
        ``MessagesEvent`` / ``TaskEndEvent`` → ``ReplanEvent`` → ``SummaryEvent``。

    Raises:
        PlanValidationError: 计划结构性非法（此时 ``PlanEvent`` 已产出，前端仍能看到坏计划）。
    """
    run_id = run_id or uuid4().hex[:12]
    thread_id = thread_id or uuid4().hex[:12]
    # 在调用 planner **之前**产出：客户端立刻拿到会话标识与用户气泡，而 planner 失败
    # 时这次运行也留得下痕迹（journal 里已有 run_start，不是一片空白）。
    yield RunStartEvent(
        run_id=run_id,
        thread_id=thread_id,
        request=request,
        started_at=datetime.now(UTC).isoformat(),
    )

    plan = await planner(request)
    yield PlanEvent(tasks=plan.tasks)
    validate_plan(plan)

    status: dict[str, str] = {task.id: "pending" for task in plan.tasks}
    results: dict[str, TaskResult] = {}
    stopped = False

    while True:
        batch = _ready_tasks(plan.tasks, status)
        if not batch:
            break

        # 先把整批的 task_start 发完（按计划顺序，稳定可测），再统一派发。
        for task in batch:
            status[task.id] = "running"
            yield TaskStartEvent(task_id=task.id, description=task.description, depends_on=task.depends_on)

        # 依赖结果取自**本批之前**的 results：同批任务之间没有依赖，这是保证的。
        invocations = [
            TaskInvocation(
                task=task,
                dependency_results=tuple(results[dep] for dep in task.depends_on if dep in results),
                run_id=run_id,
                thread_id=thread_id,
            )
            for task in batch
        ]
        crashed: dict[str, BaseException] = {}

        async for item in merge_named_streams([(inv.task.id, executor(inv)) for inv in invocations]):
            if isinstance(item, StreamCrashed):
                crashed[item.name] = item.error
                continue
            if isinstance(item, TaskEndEvent):
                results[item.task_id] = TaskResult(
                    task_id=item.task_id,
                    status=item.status,
                    output=item.output,
                    error=item.error,
                )
                status[item.task_id] = item.status
            yield item

        for task in batch:
            if status[task.id] != "running":
                continue
            # 执行单元中途没了（既没发终态也没抛），或者它自己抛了（不合约定）：
            # 都要留痕，不能让任务静默消失。
            error = _missing_terminal_error(crashed.get(task.id))
            status[task.id] = "failed"
            results[task.id] = TaskResult(task_id=task.id, status="failed", error=error)
            yield TaskEndEvent(task_id=task.id, status="failed", error=error)

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
