"""Plan-Exec 测试替身。

为什么要替身：``FakeListChatModel.with_structured_output`` 抛 ``NotImplementedError``，
所以 planner / replanner 无法用假模型驱动，只能注入假实现——这正是编排器把三者
都做成可注入依赖的价值。

``ToolCapableFake`` 另有用处：``FakeListChatModel.bind_tools`` 也抛
``NotImplementedError``，而它发生在 model call 时刻、会被 ``LLMErrorHandlingMiddleware``
兜成一段文案，**测试不会失败而是悄悄走偏**。凡是要传工具的测试都得用它。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool

from evaharness.agents.plan_exec.events import MessagesEvent, RunEvent, TaskEndEvent
from evaharness.agents.plan_exec.executor import TaskInvocation
from evaharness.agents.plan_exec.schemas import Plan, ReplanDecision, TaskResult


class ToolCapableFake(GenericFakeChatModel):
    """会响应 ``bind_tools`` 的假模型（``GenericFakeChatModel`` 本身不实现它）。"""

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any):  # noqa: ANN201
        return self


class RecordingFake(ToolCapableFake):
    """记录收到的最后一条 HumanMessage，用于断言 prompt 渲染。

    必须挂在 ``_astream`` 上：``GenericFakeChatModel`` 只实现了流式路径，覆盖
    ``_agenerate`` 根本不会被调用（写错了测试会静默地永远拿到 ``None``）。
    """

    last_human: str | None = None

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001, ANN003
        for message in reversed(list(messages)):
            if isinstance(message, HumanMessage):
                self.last_human = str(message.content)
                break
        async for chunk in super()._astream(messages, stop, run_manager, **kwargs):
            yield chunk


class StructuredFake(ToolCapableFake):
    """支持 ``with_structured_output`` 的假模型：返回预设结果并记录入参。

    ``FakeListChatModel`` / ``GenericFakeChatModel`` 都不实现结构化输出（抛
    ``NotImplementedError``），所以 planner / replanner 的「拿到结果之后怎么用」
    只能用这个替身来测——「怎么绑定」则用真实的 ``ChatOpenAI`` 测（见
    test_plan_exec_agents.py）。
    """

    result: Any = None
    seen_messages: list[Any] | None = None
    structured_method: str | None = None

    def __init__(self, **kwargs: Any) -> None:
        # messages 是父类的必填字段；结构化输出路径用不到它，给个占位即可。
        kwargs.setdefault("messages", iter([AIMessage(content="unused")]))
        super().__init__(**kwargs)

    def with_structured_output(self, schema, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.structured_method = kwargs.get("method")
        model = self

        async def _call(messages):  # noqa: ANN001, ANN202
            model.seen_messages = list(messages)
            return model.result

        return RunnableLambda(_call)


@tool
def echo(text: str) -> str:
    """回显给定文本（占位工具，用于让 bind_tools 真正被调用）。"""
    return text


# --------------------------------------------------------------------------- #
# planner / replanner
# --------------------------------------------------------------------------- #


class FakePlanner:
    """按剧本返回计划，或抛异常。记录调用次数与入参。"""

    def __init__(self, plan: Plan | None = None, *, error: BaseException | None = None) -> None:
        self.plan = plan
        self.error = error
        self.calls: list[str] = []

    async def __call__(self, request: str, /) -> Plan:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        assert self.plan is not None, "FakePlanner 需要 plan 或 error"
        return self.plan


class FakeReplanner:
    """按剧本依次给出判定；剧本用尽后重复最后一条。"""

    def __init__(self, decisions: Sequence[ReplanDecision] | None = None) -> None:
        self.decisions = list(decisions or [ReplanDecision(should_continue=True, reason="继续")])
        self.calls: list[tuple[str, list[TaskResult]]] = []

    async def __call__(self, request: str, plan: Plan, results: Sequence[TaskResult], /) -> ReplanDecision:
        self.calls.append((request, list(results)))
        if len(self.calls) <= len(self.decisions):
            return self.decisions[len(self.calls) - 1]
        return self.decisions[-1]


# --------------------------------------------------------------------------- #
# executor
# --------------------------------------------------------------------------- #


@dataclass
class ExecutorStep:
    """某个任务被执行时的剧本。"""

    messages: tuple[str, ...] = ()
    output: str | None = "任务完成"
    status: str = "done"
    error: str | None = None
    emit_terminal: bool = True
    raises: BaseException | None = None
    hangs: bool = False


@dataclass
class FakeExecutor:
    """按 task id 播放剧本的假执行单元。"""

    steps: dict[str, ExecutorStep] = field(default_factory=dict)
    default: ExecutorStep = field(default_factory=ExecutorStep)
    invocations: list[TaskInvocation] = field(default_factory=list)
    saw_cancellation: bool = False
    _hang: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def __call__(self, invocation: TaskInvocation, /) -> AsyncIterator[RunEvent]:
        self.invocations.append(invocation)
        return self._stream(invocation)

    async def _stream(self, invocation: TaskInvocation) -> AsyncIterator[RunEvent]:
        step = self.steps.get(invocation.task.id, self.default)
        try:
            for content in step.messages:
                yield MessagesEvent(chunk={"content": content, "type": "AIMessageChunk"})
            if step.raises is not None:
                raise step.raises
            if step.hangs:
                await self._hang.wait()
            if step.emit_terminal:
                yield TaskEndEvent(
                    task_id=invocation.task.id,
                    status=step.status,
                    output=step.output,
                    error=step.error,
                )
        except asyncio.CancelledError:
            self.saw_cancellation = True
            raise


async def collect(stream: AsyncIterator[RunEvent]) -> list[RunEvent]:
    """把一个事件流收干成列表。"""
    return [event async for event in stream]
