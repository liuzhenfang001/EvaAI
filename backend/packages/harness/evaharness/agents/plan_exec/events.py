"""RunEvent：一次运行的对外事件流。

这是所有路径（lead agent 与封存的 Plan-Exec）的**唯一输出通道**，也是 SSE 层与
RunJournal 共同的输入。事件全部 JSON 可序列化（不放 LangChain 对象），因此 SSE 映射
是一行透传，RunJournal 可以直接落盘。

.. note::
   模块路径 ``agents/plan_exec/events.py`` 是**遗留**：T8 起 Plan-Exec 不再是默认路径，
   而这份契约是两条路径共用的。搬到 ``agents/events.py`` 才是正确的位置，但那会牵动
   十几个 import 与 ``schemas`` 的类型归属，本片按「不扩大范围」留下，欠账记在 ADR-0010。

刻意**没有 ``error`` 变体**：任务级失败走 ``TaskEndEvent(status="failed")``
（best-effort），致命异常直接向上抛、由 SSE 层兜成 error 帧——避免两条错误通道
在同一层打架。

**``category`` 是给消费者路由用的桶，不是排他分区**（沿用参照项目 DeerFlow 的定义）：

- ``message``：承载**对话内容**——用户请求（``run_start``）、助手正文增量
  （``messages``）、任务的产出与成败（``task_end``）、展示给用户的文件
  （``files_presented``）、一轮的收尾（``run_end``）。
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
    mode: Literal["react", "plan"] = "plan"
    """这一轮走的是哪条路径（T8）。

    缺省 ``"plan"`` 是**历史回退**而不是语义缺省：T7 期间写下的 journal 行里没有这个
    字段，而那些行确实都是 Plan-Exec 写的。前端靠它决定署名、面板文案与「规划中…」
    占位，回放也能从 journal 学到同一个值——所以它必须随事件落盘。
    """


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


class PresentedFile(BaseModel):
    """一个交付给用户的文件。

    ``path`` 是**虚拟路径口径**（``/output/report.md``）——宿主路径永远不进事件、不进模型
    上下文、更不出前端（见 ``app/tools/sandbox.py`` 的模块说明）。``name`` 单独给一份而不是
    让前端再切一次路径：展示用的东西不该由消费者各自解析一遍。
    """

    path: str
    name: str
    size: int
    mime: str | None = None


class FilesPresentedEvent(_MessageEvent):
    """模型把 ``/output`` 下的成品显式呈现给用户（T11）。

    归 ``message`` 桶：它是「这一轮产出了什么」的对话内容，卡片要出现在聊天区——语义与
    :class:`TaskEndEvent` 一致（产出与成败归 message），**不是**编排过程。

    ``task_id`` 由 runner 填（见 :func:`~evaharness.agents.lead_agent.runner.run_lead_agent`），
    不由产出这个事件的工具填：泳道是 runner 的职责（``messages`` 事件也是它硬编码
    ``LEAD_LANE``），而且 ``events`` 这个模块 import ``LEAD_LANE`` 会与
    :mod:`~evaharness.agents.lead_agent.agent` 循环依赖。

    **刻意不带 ``download_url``**：URL 的形状是网关层的路由细节，harness 的契约不该知道
    HTTP 长什么样。前端拿 ``threadId`` + ``path`` 自己拼。
    """

    type: Literal["files_presented"] = "files_presented"
    task_id: str
    files: list[PresentedFile]


class RunEndEvent(_MessageEvent):
    """一次运行的收尾事件，与 :class:`RunStartEvent` 对称（T8）。

    lead agent 路径**没有** ``task_end``——它不拆任务——所以 message 桶里到它为止就
    再也没有一个终态事件，而「助手这一轮说完了」正是对话要显示的东西。

    它同时是 **journal 的提交点**：``RunJournal`` 按条目数计的阈值在归并之后很少触发，
    收尾强制 flush 才能把崩溃窗口限制在单次运行内（原先只有 ``task_end`` 有这个待遇）。

    Plan-Exec 的终态仍是 :class:`SummaryEvent`（载荷是任务级结果），形状不同，不挪用。
    """

    type: Literal["run_end"] = "run_end"
    status: Literal["completed", "failed", "cancelled"] = "completed"
    output: str | None = None
    error: str | None = None


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
    RunStartEvent
    | MessagesEvent
    | PlanEvent
    | TaskStartEvent
    | TaskEndEvent
    | ReplanEvent
    | SummaryEvent
    | FilesPresentedEvent
    | RunEndEvent,
    Field(discriminator="type"),
]
"""编排器产出的事件联合类型。"""

RunEventAdapter: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)
"""按 ``type`` 判别解析 RunEvent（SSE / RunJournal 入口用）。"""


__all__ = [
    "EventCategory",
    "FilesPresentedEvent",
    "MessagesEvent",
    "PlanEvent",
    "PresentedFile",
    "ReplanEvent",
    "RunEndEvent",
    "RunEvent",
    "RunEventAdapter",
    "RunStartEvent",
    "SummaryEvent",
    "TaskEndEvent",
    "TaskStartEvent",
]
