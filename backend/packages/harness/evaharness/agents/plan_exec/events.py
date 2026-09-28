"""RunEvent：一次 Plan-Exec 运行的对外事件流。

这是编排器的**唯一输出通道**，也是 SSE 层唯一的输入。事件全部 JSON 可序列化
（不放 LangChain 对象），因此 SSE 映射是一行透传，T3 的 RunJournal 可以直接落盘。

刻意**没有 ``error`` 变体**：任务级失败走 ``TaskEndEvent(status="failed")``
（best-effort），致命异常直接向上抛、由 SSE 层兜成 error 帧——避免两条错误通道
在同一层打架。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

from evaharness.agents.plan_exec.schemas import PlanTask, TaskResult, TerminalTaskStatus


class MessagesEvent(BaseModel):
    """ReAct 执行单元的 token 增量。

    沿用 T1 的线上形状：SSE 帧的 data 为 ``[chunk, metadata]``。
    """

    type: Literal["messages"] = "messages"
    chunk: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)


class PlanEvent(BaseModel):
    """planner 产出的结构化计划。"""

    type: Literal["plan"] = "plan"
    tasks: list[PlanTask]


class TaskStartEvent(BaseModel):
    """一个任务开始执行。"""

    type: Literal["task_start"] = "task_start"
    task_id: str
    description: str
    depends_on: list[str] = Field(default_factory=list)


class TaskEndEvent(BaseModel):
    """一个任务到达终态（done / failed / skipped）。"""

    type: Literal["task_end"] = "task_end"
    task_id: str
    status: TerminalTaskStatus
    output: str | None = None
    error: str | None = None


class ReplanEvent(BaseModel):
    """replanner 的判定。

    判定本身不改变计划，但它是「为什么剩下的任务没跑」的唯一答案——不产出这个
    事件，用户就只能看到一串「已跳过」而没有任何解释。
    """

    type: Literal["replan"] = "replan"
    should_continue: bool
    reason: str = ""


class SummaryEvent(BaseModel):
    """运行收尾的确定性汇总（不调 LLM）。

    载荷是结构化数据而非渲染好的字符串，后续想加 LLM 综述时是加字段而不是改契约。
    """

    type: Literal["summary"] = "summary"
    status: Literal["completed", "partial", "stopped"]
    results: list[TaskResult]
    failed: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)


RunEvent = Annotated[
    MessagesEvent | PlanEvent | TaskStartEvent | TaskEndEvent | ReplanEvent | SummaryEvent,
    Field(discriminator="type"),
]
"""编排器产出的事件联合类型。"""

RunEventAdapter: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)
"""按 ``type`` 判别解析 RunEvent（SSE / RunJournal 入口用）。"""


__all__ = [
    "MessagesEvent",
    "PlanEvent",
    "ReplanEvent",
    "RunEvent",
    "RunEventAdapter",
    "SummaryEvent",
    "TaskEndEvent",
    "TaskStartEvent",
]
