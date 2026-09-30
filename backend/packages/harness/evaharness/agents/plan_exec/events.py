"""RunEvent：一次 Plan-Exec 运行的对外事件流。

这是编排器的**唯一输出通道**，也是 SSE 层与 RunJournal 共同的输入。事件全部 JSON
可序列化（不放 LangChain 对象），因此 SSE 映射是一行透传，RunJournal 可以直接落盘。

刻意**没有 ``error`` 变体**：任务级失败走 ``TaskEndEvent(status="failed")``
（best-effort），致命异常直接向上抛、由 SSE 层兜成 error 帧——避免两条错误通道
在同一层打架。

**``category`` 是给消费者路由用的桶，不是排他分区**（沿用参照项目 DeerFlow 的定义）：

- ``message``：承载**对话内容**——用户请求（``run_start``）、助手正文增量
  （``messages``）、任务的产出与成败（``task_end``）。
- ``trace``：只讲**编排过程**——``plan`` / ``task_start`` / ``replan`` / ``summary``。

所以「对话投影」= 过滤 ``category == "message"``；而「trace 投影」= **全部事件**
（面板要靠 ``messages`` 里的工具调用行与正文才能重建）。这处不对称是有意的：
``category`` 回答的是「这条事件该投给谁」，不是「它属于哪个互斥分类」。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

from evaharness.agents.plan_exec.schemas import PlanTask, TaskResult, TerminalTaskStatus

EventCategory = Literal["message", "trace"]
"""事件路由桶。取值见模块 docstring。"""


class _MessageEvent(BaseModel):
    """对话内容类事件的公共标记（``category="message"``）。"""

    category: Literal["message"] = "message"


class _TraceEvent(BaseModel):
    """编排过程类事件的公共标记（``category="trace"``）。"""

    category: Literal["trace"] = "trace"


class RunStartEvent(_MessageEvent):
    """一次运行的开场事件，也是每个 run 的第一个事件。

    它承载 run 的身份（``run_id`` / ``thread_id``）与**用户原始请求**。后者是对话
    历史里「用户说了什么」的唯一来源——用户请求不是 RunEvent，所以在此之前 journal
    里根本没有它（ADR-0008 记下的缺口，ADR-0009 补上）。

    在调用 planner **之前**就产出：客户端因此能立刻拿到 thread_id 与用户气泡，
    而 planner 失败时这次运行也留得下痕迹。
    """

    type: Literal["run_start"] = "run_start"
    run_id: str
    thread_id: str
    request: str
    started_at: str


class MessagesEvent(_MessageEvent):
    """ReAct 执行单元的 token 增量。

    沿用 T1 的线上形状：SSE 帧的 data 为 ``[chunk, metadata]``（``task_id`` 由 SSE
    层并进 ``metadata``，所以线上形状一字未改）。

    ``task_id`` 是 T3 加的：并行派发后多个任务的 token 会交错到达，没有归属信息
    前端就无法把增量分流到各自的气泡与时间线泳道。

    ``metadata`` 保持**完整**（含 langgraph 的节点/步骤信息）。它是逐 token 重复的
    大头，但落盘保留是刻意的决定（可逆的那一边），体积问题由 RunJournal 的
    **写盘归并**解决，不靠削字段。
    """

    type: Literal["messages"] = "messages"
    task_id: str
    chunk: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)


class PlanEvent(_TraceEvent):
    """planner 产出的结构化计划。

    T7 起**不再带 ``run_id``**——run 的身份统一由 :class:`RunStartEvent` 承载
    （T3 把这笔债记在 ADR-0008 里，这里是还债）。
    """

    type: Literal["plan"] = "plan"
    tasks: list[PlanTask]


class TaskStartEvent(_TraceEvent):
    """一个任务开始执行。"""

    type: Literal["task_start"] = "task_start"
    task_id: str
    description: str
    depends_on: list[str] = Field(default_factory=list)


class TaskEndEvent(_MessageEvent):
    """一个任务到达终态（done / failed / skipped）。

    归 ``message`` 桶：它带着该任务的**产出**与成败，是对话里「助手说了什么、
    有没有成」的来源之一。
    """

    type: Literal["task_end"] = "task_end"
    task_id: str
    status: TerminalTaskStatus
    output: str | None = None
    error: str | None = None


class ReplanEvent(_TraceEvent):
    """replanner 的判定。

    判定本身不改变计划，但它是「为什么剩下的任务没跑」的唯一答案——不产出这个
    事件，用户就只能看到一串「已跳过」而没有任何解释。
    """

    type: Literal["replan"] = "replan"
    should_continue: bool
    reason: str = ""


class SummaryEvent(_TraceEvent):
    """运行收尾的确定性汇总（不调 LLM）。

    载荷是结构化数据而非渲染好的字符串，后续想加 LLM 综述时是加字段而不是改契约。
    """

    type: Literal["summary"] = "summary"
    status: Literal["completed", "partial", "stopped"]
    results: list[TaskResult]
    failed: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)


RunEvent = Annotated[
    RunStartEvent | MessagesEvent | PlanEvent | TaskStartEvent | TaskEndEvent | ReplanEvent | SummaryEvent,
    Field(discriminator="type"),
]
"""编排器产出的事件联合类型。"""

RunEventAdapter: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)
"""按 ``type`` 判别解析 RunEvent（SSE / RunJournal 入口用）。"""


__all__ = [
    "EventCategory",
    "MessagesEvent",
    "PlanEvent",
    "ReplanEvent",
    "RunEvent",
    "RunEventAdapter",
    "RunStartEvent",
    "SummaryEvent",
    "TaskEndEvent",
    "TaskStartEvent",
]
