"""ReAct 任务执行单元：把「计划里的一个任务」翻译成一次 ReAct 图运行。

这是 Plan-Exec 与 ReAct 的嵌套点（ADR-0004）：编排器只管派发，任务内部的
推理-行动循环完全交给 :func:`evaharness.agents.react.make_react_agent`。

两个易错点在这里被一次性钉死：

1. **``thread_id`` 必须同时传给 ``config`` 与 ``context``**。接了 checkpointer 之后
   只传 ``context`` 会直接 ``ValueError``（checkpointer 从 ``configurable`` 取键）；
   而 ``LoopDetectionMiddleware`` 读的是 ``runtime.context``，且按 thread_id 分桶、
   进程内不重置——多个任务共用一个桶会累加到硬上限，把正常的工具调用误判成循环。
2. **``stream_mode=["messages","values"]`` 的载荷是嵌套元组**：外层 ``(mode, payload)``，
   而 ``messages`` 的 payload 自身又是 ``(chunk, metadata)``。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from langchain.agents.middleware import AgentMiddleware
from langchain.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langgraph.graph.state import CompiledStateGraph

from evaharness.agents.messages import final_text
from evaharness.agents.plan_exec.events import MessagesEvent, RunEvent, TaskEndEvent
from evaharness.agents.plan_exec.schemas import PlanTask, TaskResult
from evaharness.agents.react import make_react_agent
from evaharness.checkpoint import task_thread_id

DEFAULT_EXECUTOR_PROMPT = (
    "You are a task executor inside a larger plan. Complete the single task you are given, "
    "using the available tools when they help. Answer in the same language as the task. "
    "Be concise and report the concrete result."
)


@dataclass(frozen=True, slots=True)
class TaskInvocation:
    """一次任务派发所需的全部输入。"""

    task: PlanTask
    dependency_results: tuple[TaskResult, ...]
    run_id: str
    thread_id: str


class TaskExecutor(Protocol):
    """任务执行单元：吃一个 :class:`TaskInvocation`，吐一串 :class:`RunEvent`。"""

    def __call__(self, invocation: TaskInvocation, /) -> AsyncIterator[RunEvent]: ...


def render_task_prompt(task: PlanTask, dependency_results: Sequence[TaskResult]) -> str:
    """把任务与上游结果渲染成执行单元要看的用户消息。

    上游结果必须带上：不带的话 ``depends_on`` 就只是画在面板上的装饰，下游任务
    根本不知道上游产出了什么。
    """
    lines = [f"任务：{task.description}"]
    if task.completion_criteria:
        lines.append("")
        lines.append("完成标准：")
        lines.extend(f"- {criterion}" for criterion in task.completion_criteria)
    if dependency_results:
        lines.append("")
        lines.append("上游任务结果（供参考）：")
        for result in dependency_results:
            lines.append("")
            lines.append(f"### {result.task_id}")
            if result.output:
                lines.append(result.output)
            elif result.error:
                lines.append(f"（上游未成功：{result.error}）")
            else:
                lines.append("（上游未产出内容）")
    return "\n".join(lines)


def make_react_executor(
    model: BaseChatModel,
    tools: Sequence[Any] | None = None,
    *,
    system_prompt: str | None = None,
    middleware: Sequence[AgentMiddleware] | None = None,
    checkpointer: Any | None = None,
    **kwargs: Any,
) -> TaskExecutor:
    """构建基于 ReAct 的默认执行单元。

    图在本执行单元**实例内**建一次，该实例的所有任务共用（有 checkpointer 时状态按
    thread 隔离，无 checkpointer 时每任务本来就是全新输入），T3 的并行批次也共享同一个
    图——所以**别为了省 2.2 ms 去复用执行单元**：图被 ``execute`` 闭包持有，复用等于
    把它的中间件实例变成长命共享物（ADR-0011）。业务层的 ``AgentFactory.plan_exec()``
    每个请求建一个新实例。
    """
    graph = make_react_agent(
        model,
        tools,
        system_prompt=system_prompt or DEFAULT_EXECUTOR_PROMPT,
        middleware=middleware,
        checkpointer=checkpointer,
        **kwargs,
    )

    async def execute(invocation: TaskInvocation, /) -> AsyncIterator[RunEvent]:
        task = invocation.task
        # checkpoint 的坐标：会话 + 运行 + 任务，三者缺一不可（见 task_thread_id）。
        thread_id = task_thread_id(invocation.thread_id, invocation.run_id, task.id)
        payload = {"messages": [HumanMessage(content=render_task_prompt(task, invocation.dependency_results))]}
        last_state: Mapping[str, Any] | None = None
        try:
            async for item in _astream(graph, payload, thread_id):
                kind, data = item
                if kind == "messages":
                    chunk, metadata = data
                    yield MessagesEvent(
                        task_id=task.id,
                        chunk=chunk.model_dump(mode="json"),
                        metadata=metadata if isinstance(metadata, dict) else {},
                    )
                else:
                    last_state = data
        except Exception as exc:  # 非 CancelledError：模型错误已由中间件兜住，这里是图级失败的网
            yield TaskEndEvent(
                task_id=task.id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        yield TaskEndEvent(task_id=task.id, status="done", output=final_text(last_state))

    return execute


def _astream(
    graph: CompiledStateGraph,
    payload: Mapping[str, Any],
    thread_id: str,
) -> AsyncIterator[tuple[str, Any]]:
    """统一多模式流式的载荷形状，便于 :func:`make_react_executor` 保持扁平。"""
    return graph.astream(
        payload,
        config={"configurable": {"thread_id": thread_id}},
        context={"thread_id": thread_id},
        stream_mode=["messages", "values"],
    )


__all__ = [
    "DEFAULT_EXECUTOR_PROMPT",
    "TaskExecutor",
    "TaskInvocation",
    "final_text",
    "make_react_executor",
    "render_task_prompt",
]
