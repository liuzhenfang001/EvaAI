"""Plan-Exec 的数据契约：计划、任务、结果、replan 决策。

状态词表分两层：``pending`` / ``running`` 是**面板态**，由编排器本地维护、不进
``TaskResult``（只有终态才落进结果）；``done`` / ``failed`` / ``skipped`` 是终态。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

TerminalTaskStatus = Literal["done", "failed", "skipped"]


class PlanTask(BaseModel):
    """计划中的单个任务。

    刻意保持扁平（无嵌套对象）：planner 走 function calling 产出结构化输出，
    嵌套层级越深越容易漏字段。
    """

    id: str
    description: str
    completion_criteria: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    """planner 产出的任务图。"""

    tasks: list[PlanTask]


class TaskResult(BaseModel):
    """单个任务的终态结果。"""

    task_id: str
    status: TerminalTaskStatus
    output: str | None = None
    error: str | None = None


class ReplanDecision(BaseModel):
    """replanner 的判定：继续还是结束（不重写计划）。"""

    should_continue: bool = Field(description="是否还有必要继续执行剩余任务")
    reason: str = Field(default="", description="判定理由")


class PlanValidationError(Exception):
    """计划结构性非法（空计划 / 重复 id / 自依赖 / 依赖环）。"""


def validate_plan(plan: Plan) -> None:
    """校验计划的结构合法性，非法则抛 :class:`PlanValidationError`。

    拦截的是 planner 的**结构性 bug**：空计划、重复 id、自依赖、依赖环——这些
    要么无法执行，要么会让编排器静默空转，报出来比咽下去更有用。

    **悬空依赖不在此列**：那是 LLM 的幻觉噪音，让该任务永远不 ready、收尾时自然
    变成 ``skipped``，比抛异常（整个计划作废）更符合 best-effort 语义。
    """
    tasks = plan.tasks
    if not tasks:
        raise PlanValidationError("plan must contain at least one task")

    ids = [task.id for task in tasks]
    duplicates = sorted({task_id for task_id in ids if ids.count(task_id) > 1})
    if duplicates:
        raise PlanValidationError(f"duplicate task ids: {', '.join(duplicates)}")

    known = set(ids)
    for task in tasks:
        if task.id in task.depends_on:
            raise PlanValidationError(f"task {task.id!r} must not depend on itself")

    # 迭代式 DFS：三色标记，遇到灰色节点即成环。
    edges = {task.id: [dep for dep in task.depends_on if dep in known] for task in tasks}
    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(ids, WHITE)
    for start in ids:
        if colour[start] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        colour[start] = GREY
        while stack:
            node, index = stack[-1]
            if index < len(edges[node]):
                stack[-1] = (node, index + 1)
                nxt = edges[node][index]
                if colour[nxt] == GREY:
                    raise PlanValidationError(f"dependency cycle detected involving {nxt!r}")
                if colour[nxt] == WHITE:
                    colour[nxt] = GREY
                    stack.append((nxt, 0))
            else:
                colour[node] = BLACK
                stack.pop()


__all__ = [
    "Plan",
    "PlanTask",
    "PlanValidationError",
    "ReplanDecision",
    "TaskResult",
    "TerminalTaskStatus",
    "validate_plan",
]
