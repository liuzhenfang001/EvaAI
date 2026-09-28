"""replanner：每批任务结束后判定「继续还是结束」。

**不重写计划**——这是刻意的职责收窄（见 CONTEXT.md 词条）。判定所需的一切都已
在上下文里：原始请求、计划、到目前为止的结果。判定失败/被跳过的情况同样带上，
否则它无从判断「剩下的还有没有必要跑」。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from langchain.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from evaharness.agents.plan_exec.schemas import Plan, ReplanDecision, TaskResult

DEFAULT_REPLANNER_PROMPT = (
    "You decide whether the remaining tasks of a plan still need to run.\n"
    "You do NOT rewrite or extend the plan.\n"
    "Return should_continue=false when the user's request is already satisfied by the results so far, "
    "or when the remaining tasks can no longer be attempted."
)


class Replanner(Protocol):
    """判定继续/结束的角色。"""

    async def __call__(self, request: str, plan: Plan, results: Sequence[TaskResult], /) -> ReplanDecision: ...


def render_replan_context(request: str, plan: Plan, results: Sequence[TaskResult]) -> str:
    """把请求、计划与已得结果渲染成 replanner 要看的文本。"""
    lines = [f"用户请求：{request}", "", "计划："]
    lines.extend(f"- [{result_status(results, task.id)}] {task.id}：{task.description}" for task in plan.tasks)
    lines.append("")
    lines.append("已得结果：")
    if not results:
        lines.append("（无）")
    for result in results:
        lines.append(f"- {result.task_id}：{result.status}")
        if result.output:
            lines.append(f"  {result.output}")
        if result.error:
            lines.append(f"  错误：{result.error}")
    return "\n".join(lines)


def result_status(results: Sequence[TaskResult], task_id: str) -> str:
    """任务在上下文里的当前状态（未出现即 pending）。"""
    for result in results:
        if result.task_id == task_id:
            return result.status
    return "pending"


class ModelReplanner:
    """基于模型结构化输出的 replanner。"""

    def __init__(
        self,
        model: BaseChatModel,
        *,
        system_prompt: str | None = None,
        method: str = "function_calling",
    ) -> None:
        self.model = model
        self.method = method
        # None = 未指定（用默认提示词）；"" = 显式不要系统消息。
        self.system_prompt = DEFAULT_REPLANNER_PROMPT if system_prompt is None else system_prompt
        self.runnable = model.with_structured_output(ReplanDecision, method=method)

    def _messages(self, request: str, plan: Plan, results: Sequence[TaskResult]) -> list[Any]:
        messages: list[Any] = []
        if self.system_prompt:
            messages.append(SystemMessage(content=self.system_prompt))
        messages.append(HumanMessage(content=render_replan_context(request, plan, results)))
        return messages

    async def __call__(self, request: str, plan: Plan, results: Sequence[TaskResult], /) -> ReplanDecision:
        # 「还有没有必要跑」由编排器判断（没有就绪任务时根本不会问），这里只管问模型。
        return await self.runnable.ainvoke(self._messages(request, plan, results))


def make_replanner(
    model: BaseChatModel,
    *,
    system_prompt: str | None = None,
    method: str = "function_calling",
) -> Replanner:
    """构建 replanner。"""
    return ModelReplanner(model, system_prompt=system_prompt, method=method)


__all__ = [
    "DEFAULT_REPLANNER_PROMPT",
    "ModelReplanner",
    "Replanner",
    "make_replanner",
    "render_replan_context",
]
