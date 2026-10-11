"""跑一次主 Agent，把图的输出翻成 RunEvent 流。

这是**唯一**的运行入口（Plan-Exec 那条在 T16 下线了，见 ADR-0021）。事件序列以
``run_start`` 开场、以终态事件收尾，中间是 ``messages`` 增量——这里**没有 plan、
没有 task_start / task_end**，因为主 Agent 不拆任务；收尾是 :class:`RunEndEvent`。

三个必须一起给对的东西（错一个就是难查的怪症状）：

1. **checkpoint 坐标必须同时进 ``config`` 与 ``context``**。checkpointer 从
   ``config.configurable.thread_id`` 取键，缺了直接 ``ValueError``；而
   ``LoopDetectionMiddleware`` / ``TerminalResponseMiddleware`` 读的是
   ``runtime.context``，且按它分桶。两者给成不同的值，就会出现「检查点写在 A、
   循环检测记在 B」。
2. **坐标是会话级且跨轮固定的**（``lead_thread_id``）——这正是记忆的来源：坐标不变，
   checkpointer 才会在下一轮把上一轮的消息一起交给模型。
3. **``stream_mode=["messages","values"]`` 的载荷是嵌套元组**：外层 ``(mode, payload)``，
   而 ``messages`` 的 payload 自身又是 ``(chunk, metadata)``。

**主动取消**（T5）把「内层 langgraph 流」搬进一个显式 ``asyncio.Task``（pump），
外层生成器经队列消费——这是 ADR-0006 里 fanout.py 的先例（``create_task`` + sentinel，
**不用** ``TaskGroup``，因为消费方提前放弃时它会抛 ``BaseExceptionGroup``）：

- **主动取消**：取消端点触达 pump 并 ``pump.cancel()`` → pump 放进 ``cancelled``
  sentinel 后 re-raise；外层读到 sentinel、``gather`` 确认 pump 已 cancelled →
  ``yield RunEndEvent(status="cancelled")``。**外层响应 task 仍存活**，这个终态才能经
  SSE 送给还连着的前端，再正常收尾。
- **被动断连**：Starlette 取消外层响应 task → ``CancelledError`` 在 ``queue.get()``
  处穿透（它继承 ``BaseException``，``except Exception`` 接不住）；外层 ``finally``
  取消还没结束的 pump 并 ``unregister``，**不产** ``cancelled`` 事件——断开不该被
  伪装成一次「失败」或「取消」的运行（ADR-0005）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from langchain_core.messages import HumanMessage
from langgraph.graph.state import CompiledStateGraph
from pydantic import ValidationError

from evaharness.agents.events import (
    FilesPresentedEvent,
    MessagesEvent,
    PresentedFile,
    RunEndEvent,
    RunEvent,
    RunStartEvent,
)
from evaharness.agents.lead_agent.agent import LEAD_LANE, LeadContext
from evaharness.agents.lead_agent.custom_events import PRESENT_FILES_KIND
from evaharness.agents.messages import final_text
from evaharness.agents.options import RunOptions
from evaharness.checkpoint import lead_thread_id
from evaharness.sandbox.paths import SandboxLayout

logger = logging.getLogger(__name__)


class RunCancellation:
    """主动取消的接缝：runner 把驱动内层流的 task 交进来，由网关层接进 run registry。

    这里只声明两个钩子、**不 import app**——harness 不知道 registry 长什么样，也不该
    知道（守住 ``test_harness_boundary.py``）。缺省 no-op，让「不取消」的调用方（测试、
    封存的 plan 路径）不必传。
    """

    def __init__(self, register=None, unregister=None) -> None:
        self._register = register or (lambda _: None)
        self._unregister = unregister or (lambda: None)

    def register(self, task: asyncio.Task) -> None:
        """把驱动本轮的内层 task 交出去（网关层据此按 run_id 触达它）。"""
        self._register(task)

    def unregister(self) -> None:
        """运行结束（无论何种终态）时摘除登记，避免 registry 泄漏。"""
        self._unregister()


async def run_lead_agent(
    request: str,
    *,
    agent: CompiledStateGraph,
    run_id: str | None = None,
    thread_id: str | None = None,
    cancellation: RunCancellation | None = None,
    options: RunOptions | None = None,
    sandbox: SandboxLayout | None = None,
) -> AsyncIterator[RunEvent]:
    """跑一轮主 Agent。

    Args:
        request: 用户这一轮的原始输入。
        agent: :func:`~evaharness.agents.lead_agent.agent.make_lead_agent` 编译好的图。
        run_id: 本次运行的标识；缺省自生。
        thread_id: **会话**标识（不是 checkpoint 坐标，坐标由 :func:`lead_thread_id`
            派生）；缺省自生。给了就用——前端因此能先斩后奏地开一段新会话。
        sandbox: 这次运行的沙箱布局（宿主根 + 挂载表）。要用到文件工具就必须给——
            它是**部署策略**，由调用方决定，经运行上下文传给工具。不碰文件的运行可以不给。
        cancellation: 主动取消的接缝；缺省 no-op（见 :class:`RunCancellation`）。

    Yields:
        ``RunStartEvent`` → 若干 ``MessagesEvent`` → ``RunEndEvent``。
        终态三种：``completed``（正常）、``failed``（图级失败）、``cancelled``（主动取消）。

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
        mode="react",
        # 回显这一轮用的是哪档思考：回放时前端只能从这里学到，不能靠实时内存。
        thinking=options.thinking if options is not None else "off",
    )

    coordinate = lead_thread_id(thread_id)
    payload = {"messages": [HumanMessage(content=request)]}
    last_state: Mapping[str, Any] | None = None

    queue: asyncio.Queue = asyncio.Queue()
    pump = asyncio.create_task(
        _pump(
            _astream(agent, payload, session_id=thread_id, coordinate=coordinate, run_id=run_id, sandbox=sandbox),
            queue,
        )
    )
    if cancellation is not None:
        cancellation.register(pump)

    try:
        while True:
            kind, item = await queue.get()
            if kind == "eof":
                break
            if kind == "cancelled":
                # 主动取消：pump 已把 cancelled sentinel 放进队列、正要 re-raise。
                # gather 等它收尾（return_exceptions 吃掉 CancelledError），避免
                # 「Task was destroyed but it is pending」；外层仍在存活，终态得以送出。
                await asyncio.gather(pump, return_exceptions=True)
                yield RunEndEvent(status="cancelled")
                return
            if kind == "error":
                yield RunEndEvent(status="failed", error=f"{type(item).__name__}: {item}")
                return
            stream_kind, data = item
            if stream_kind == "messages":
                chunk, metadata = data
                yield MessagesEvent(
                    task_id=LEAD_LANE,
                    chunk=chunk.model_dump(mode="json"),
                    metadata=metadata if isinstance(metadata, dict) else {},
                )
            elif stream_kind == "custom":
                # 工具经 runtime.stream_writer 送出来的载荷（见 custom_events 模块）。
                # **必须排在 values 的 else 之前**——否则载荷会被当成 state 快照覆盖
                # last_state，症状是 run_end.output 莫名其妙变成一段工具载荷。
                event = _tool_event(data)
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
        if not pump.done():
            pump.cancel()

    # 图可能**挂起**而不是跑完（`interrupt()`，目前唯一来源是 MCP elicitation）。那不是异常，
    # 图安静地停下、把 `__interrupt__` 留在最后一次 values 快照里——不管它的话这一轮会以
    # `completed` 收场，模型永远等不到工具结果，而用户只看到「莫名其妙没下文」。
    pending = _pending_interrupt(last_state)
    if pending is not None:
        yield RunEndEvent(status="failed", error=pending)
        return

    yield RunEndEvent(status="completed", output=final_text(last_state))


async def _pump(stream: AsyncIterator[tuple[str, Any]], queue: asyncio.Queue) -> None:
    """把内层 langgraph 流泵进队列，并把三种结束各归成一类哨兵。

    哨兵是**显式值**而不是「事后看 task 是否 cancelled」——后者有竞态：外层读到队列
    的那一刻，pump 可能还没真正结束，``pump_task.cancelled()`` 读出来是 False，就会
    误判成「正常结束」继续往下走。
    """
    try:
        async for item in stream:
            await queue.put(("item", item))
        await queue.put(("eof", None))
    except asyncio.CancelledError:
        await queue.put(("cancelled", None))
        raise
    except Exception as exc:
        await queue.put(("error", exc))


def _astream(
    agent: CompiledStateGraph,
    payload: Mapping[str, Any],
    *,
    session_id: str,
    coordinate: str,
    run_id: str,
    sandbox: SandboxLayout | None = None,
) -> AsyncIterator[tuple[str, Any]]:
    """统一多模式流式的载荷形状，便于 :func:`run_lead_agent` 保持扁平。"""
    return agent.astream(
        payload,
        config={"configurable": {"thread_id": coordinate}},
        context=LeadContext(session_id=session_id, thread_id=coordinate, run_id=run_id, sandbox=sandbox),
        stream_mode=["messages", "values", "custom"],
    )


def _tool_event(payload: Any) -> RunEvent | None:
    """把工具经 custom 流送出的载荷翻成 ``RunEvent``；不认识的一律返回 ``None``。

    **白名单**，不是「有 ``type`` 字段就照着构造」：自定义流是开放的，任何工具都能往里写
    任意 JSON——照单全收的话，一个工具就能伪造 ``run_end`` 把本轮运行提前结束掉。所以这里
    只认自己定义过的 kind，映射关系写死在代码里。

    载荷不合法（缺字段、类型不对）时记一条警告后跳过，**不抛**：与 journal 记账失败同一个
    立场——可观测/附属通道出问题不该把用户的这一轮跑挂。
    """
    if not isinstance(payload, Mapping) or payload.get("kind") != PRESENT_FILES_KIND:
        return None
    files = payload.get("files") or []
    if not files:
        return None
    try:
        return FilesPresentedEvent(
            task_id=LEAD_LANE,
            files=[PresentedFile.model_validate(file) for file in files],
        )
    except ValidationError:
        logger.warning("工具送出的文件载荷不合法，已忽略", exc_info=True)
        return None


_GENERIC_INTERRUPT = (
    "这一轮被 interrupt() 挂起，而没有任何东西会去应答它，所以无法继续。"
    "（EvaAI 目前不实现 LangGraph 的中断/恢复，见 ADR-0017）"
)


def _pending_interrupt(state: Mapping[str, Any] | None) -> str | None:
    """最后一次 values 快照里有没有**没人应答**的挂起；有就返回一句给人看的话。

    为什么必须处理：`interrupt()` 不是异常，图完成后 `__interrupt__` 就留在 state 里，
    而 :func:`~evaharness.agents.lead_agent.agent.run_lead_agent` 的收尾只读
    ``final_text``——不在这里拦一下，整件事**没有任何痕迹**。

    为什么放在 harness：这是一条**通用**规则（挂起 ≠ 跑完），不依赖具体是谁调的
    ``interrupt()``。只有文案里那点 MCP 细节是尽人事的——认不出来也照样报，只是措辞通用。
    """
    if not isinstance(state, Mapping):
        return None
    interrupts = state.get("__interrupt__")
    if not interrupts:
        return None
    return _describe_interrupt(interrupts)


def _describe_interrupt(interrupts: Any) -> str:
    """把挂起载荷翻译成一句用户看得懂的话。

    ``langchain.mcp`` 的 payload 形状是 ``MCPElicitationInterrupt``
    （``{"type": "mcp_elicitation", "tool_name": ..., "requests": [...]}``，见
    :mod:`langchain.mcp.elicitation`）。认不出来的一律退回通用措辞——**不猜**。
    """
    for item in interrupts if isinstance(interrupts, Sequence) else [interrupts]:
        value = getattr(item, "value", None)
        if not isinstance(value, Mapping) or value.get("type") != "mcp_elicitation":
            continue
        tool = value.get("tool_name") or "某个 MCP 工具"
        asked = [
            request.get("message")
            for request in value.get("requests") or []
            if isinstance(request, Mapping) and request.get("message")
        ]
        question = f"它问的是：「{'；'.join(asked)}」" if asked else ""
        return (
            f"MCP 工具 {tool!r} 要求人工输入才能继续（elicitation），"
            f"而 EvaAI 还没有应答它的链路，所以这一轮到此为止。{question}"
        )
    return _GENERIC_INTERRUPT


__all__ = ["RunCancellation", "run_lead_agent"]
