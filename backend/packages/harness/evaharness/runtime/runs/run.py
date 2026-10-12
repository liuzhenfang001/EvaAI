"""跑一次 agent，把图的输出翻成 RunEvent 流。

这是**唯一**的运行入口。事件序列以 ``run_start`` 开场、以终态事件收尾，中间是 ``messages``
增量——这里**没有 plan、没有 task_start / task_end**，因为主 Agent 不拆任务；收尾是
:class:`RunEndEvent`。

它只吃一张**编译好的图**与一串参数，不认识「lead」是谁：泳道与路径都由 ``lane`` / ``mode``
参数给（缺省值对着主 Agent），所以同一段代码将来喂别的图也成立。

三个必须一起给对的东西（错一个就是难查的怪症状）：

1. **checkpoint 坐标必须同时进 ``config`` 与 ``context``**。checkpointer 从
   ``config.configurable.thread_id`` 取键，缺了直接 ``ValueError``；而
   ``LoopDetectionMiddleware`` / ``TerminalResponseMiddleware`` 读的是
   ``runtime.context``，且按它分桶。两者给成不同的值，就会出现「检查点写在 A、
   循环检测记在 B」。
2. **坐标是会话级且跨轮固定的**（``lead_thread_id``）——这正是记忆的来源：坐标不变，
   checkpointer 才会在下一轮把上一轮的消息一起交给模型。
3. **``stream_mode=["messages","values","custom"]`` 的载荷是三种形状混在一条流里**，
   翻译在 :mod:`evaharness.runtime.event.translate`，快照留给收尾取正文。

**主动取消**（T5）把「内层 langgraph 流」搬进一个显式 ``asyncio.Task``（见
:mod:`evaharness.runtime.runs.pump`），外层生成器经队列消费：

- **主动取消**：取消端点触达 pump 并 ``task.cancel()`` → pump 放进 ``cancelled``
  sentinel 后 re-raise；外层读到 sentinel、``gather`` 确认 pump 已 cancelled →
  ``yield RunEndEvent(status="cancelled")``。**外层响应 task 仍存活**，这个终态才能经
  SSE 送给还连着的前端，再正常收尾。
- **被动断连**：Starlette 取消外层响应 task → ``CancelledError`` 在 ``queue.get()``
  处穿透（它继承 ``BaseException``，``except Exception`` 接不住）；外层 ``finally``
  取消还没结束的 pump 并 ``unregister``，**不产** ``cancelled`` 事件——断开不该被
  伪装成一次「失败」或「取消」的运行（ADR-0005）。

.. note::
   原叫 ``run_lead_agent``、住在 ``agents/lead_agent/runner.py``（T19 搬来并泛化，见
   ADR-0024）。``lane`` / ``mode`` 原先硬编码在函数体里（``LEAD_LANE`` 与字面量 ``"react"``），
   那是它没法脱离 ``agents`` 的最后一根线——搬进来就必须参数化。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from langchain_core.messages import HumanMessage
from langgraph.graph.state import CompiledStateGraph

from evaharness.checkpoint import lead_thread_id
from evaharness.runtime.context.contract import RunContext
from evaharness.runtime.context.options import RunOptions
from evaharness.runtime.event.contract import DEFAULT_LANE, RunEndEvent, RunEvent, RunStartEvent
from evaharness.runtime.event.translate import produces_event, translate
from evaharness.runtime.runs.cancellation import RunCancellation
from evaharness.runtime.runs.pump import CANCELLED, EOF, ERROR, pump
from evaharness.runtime.runs.state import final_text, pending_interrupt

if TYPE_CHECKING:
    # 只在类型检查时导入：`evaharness.sandbox` 的 `__init__` 会拉进 bash / file_ops 工具，
    # 而它们反过来 import 本包的 `runtime.context` 取布局——运行期导入就成环
    # （与 `context/contract.py` 同一手法，见 ADR-0020 的硬约束段）。
    from evaharness.sandbox.paths import SandboxLayout

logger = logging.getLogger(__name__)


async def run_agent(
    request: str,
    *,
    agent: CompiledStateGraph,
    run_id: str | None = None,
    thread_id: str | None = None,
    cancellation: RunCancellation | None = None,
    options: RunOptions | None = None,
    sandbox: SandboxLayout | None = None,
    lane: str = DEFAULT_LANE,
    mode: Literal["react", "plan"] = "react",
) -> AsyncIterator[RunEvent]:
    """跑一轮 agent。

    Args:
        request: 用户这一轮的原始输入。
        agent: 编译好的图。
        run_id: 本次运行的标识；缺省自生。
        thread_id: **会话**标识（不是 checkpoint 坐标，坐标由 :func:`lead_thread_id`
            派生）；缺省自生。给了就用——前端因此能先斩后奏地开一段新会话。
        cancellation: 主动取消的接缝；缺省 no-op（见 :class:`RunCancellation`）。
        options: 这一轮的参数（思考档位等）；缺省按「没带参数」处理。
        sandbox: 这次运行的沙箱布局（宿主根 + 挂载表）。要用到文件工具就必须给——
            它是**部署策略**，由调用方决定，经运行上下文传给工具。不碰文件的运行可以不给。
        lane: 这一轮在事件流里的泳道 id；缺省 :data:`DEFAULT_LANE`（主 Agent 用那一条）。
        mode: 这一轮走的是哪条路径，随 ``run_start`` 回显给前端。目前只有 ``"react"``
            会被生产出来；``"plan"`` 是**回放兼容**用的历史取值。

    Yields:
        ``RunStartEvent`` → 若干 ``MessagesEvent``（或工具送出的其他事件）→ ``RunEndEvent``。
        终态三种：``completed``（正常）、``failed``（图级失败 / 无人应答的挂起）、
        ``cancelled``（主动取消）。

    Raises:
        :class:`asyncio.CancelledError`（继承 ``BaseException``）会**原样穿透**——被动
        断开时的硬取消不伪装成任何终态事件（ADR-0005）。
    """
    run_id = run_id or uuid4().hex[:12]
    thread_id = thread_id or uuid4().hex[:12]
    # 在调用模型**之前**产出：客户端立刻拿到会话标识与用户气泡，而模型失败时这次
    # 运行也留得下痕迹（journal 里已有 run_start，不是一片空白）。
    yield RunStartEvent(
        run_id=run_id,
        thread_id=thread_id,
        request=request,
        started_at=datetime.now(UTC).isoformat(),
        mode=mode,
        # 回显这一轮用的是哪档思考：回放时前端只能从这里学到，不能靠实时内存。
        thinking=options.thinking if options is not None else "off",
    )

    coordinate = lead_thread_id(thread_id)
    payload = {"messages": [HumanMessage(content=request)]}
    last_state: Mapping[str, Any] | None = None

    stream = _astream(agent, payload, session_id=thread_id, coordinate=coordinate, run_id=run_id, sandbox=sandbox)
    queue: asyncio.Queue = asyncio.Queue()
    inner = asyncio.create_task(pump(stream, queue))
    if cancellation is not None:
        cancellation.register(inner)

    try:
        while True:
            kind, item = await queue.get()
            if kind == EOF:
                break
            if kind == CANCELLED:
                # 主动取消：pump 已把 cancelled sentinel 放进队列、正要 re-raise。
                # gather 等它收尾（return_exceptions 吃掉 CancelledError），避免
                # 「Task was destroyed but it is pending」；外层仍在存活，终态得以送出。
                await asyncio.gather(inner, return_exceptions=True)
                yield RunEndEvent(status="cancelled")
                return
            if kind == ERROR:
                yield RunEndEvent(status="failed", error=f"{type(item).__name__}: {item}")
                return
            stream_kind, data = item
            # 正向判定哪些模式产事件，其余即 values 快照（见 translate 模块的 warning：
            # 别在这里把它写成「translate 返回 None 就当快照」）。
            if produces_event(stream_kind):
                event = translate(stream_kind, data, lane=lane)
                if event is not None:
                    yield event
            else:  # values
                last_state = data
    except Exception as exc:  # 仅剩串行化等处理错误：图级失败已由 pump 转成 error sentinel
        yield RunEndEvent(status="failed", error=f"{type(exc).__name__}: {exc}")
        return
    finally:
        if cancellation is not None:
            cancellation.unregister()
        if not inner.done():
            inner.cancel()

    # 图可能**挂起**而不是跑完（`interrupt()`，目前唯一来源是 MCP elicitation）。那不是异常，
    # 图安静地停下、把 `__interrupt__` 留在最后一次 values 快照里——不管它的话这一轮会以
    # `completed` 收场，模型永远等不到工具结果，而用户只看到「莫名其妙没下文」。
    pending = pending_interrupt(last_state)
    if pending is not None:
        yield RunEndEvent(status="failed", error=pending)
        return

    yield RunEndEvent(status="completed", output=final_text(last_state))


def _astream(
    agent: CompiledStateGraph,
    payload: Mapping[str, Any],
    *,
    session_id: str,
    coordinate: str,
    run_id: str,
    sandbox: SandboxLayout | None = None,
) -> AsyncIterator[tuple[str, Any]]:
    """统一多模式流式的载荷形状，便于 :func:`run_agent` 保持扁平。

    坐标**同时**给 ``config`` 与 ``context``（见模块 docstring 第 1 条），值与字段名都不能
    各写各的。``context`` 那侧是 :class:`~evaharness.runtime.context.contract.RunContext`。
    """
    return agent.astream(
        payload,
        config={"configurable": {"thread_id": coordinate}},
        context=RunContext(session_id=session_id, thread_id=coordinate, run_id=run_id, sandbox=sandbox),
        stream_mode=["messages", "values", "custom"],
    )


__all__ = ["run_agent"]
