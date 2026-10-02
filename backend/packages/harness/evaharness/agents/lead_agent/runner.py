"""跑一次主 Agent，把图的输出翻成 RunEvent 流。

与 :func:`evaharness.agents.plan_exec.run_plan_exec` 是同位角色（两条路径的入口），
所以事件序列的首尾刻意保持一致：都以 ``run_start`` 开场、以终态事件收尾，中间是
``messages`` 增量。区别只在于——这里**没有 plan、没有 task_start / task_end**，
因为主 Agent 不拆任务；收尾是 :class:`RunEndEvent`。

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
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from langchain_core.messages import HumanMessage
from langgraph.graph.state import CompiledStateGraph

from evaharness.agents.lead_agent.agent import LEAD_LANE, LeadContext
from evaharness.agents.messages import final_text
from evaharness.agents.plan_exec.events import MessagesEvent, RunEndEvent, RunEvent, RunStartEvent
from evaharness.checkpoint import lead_thread_id


async def run_lead_agent(
    request: str,
    *,
    agent: CompiledStateGraph,
    run_id: str | None = None,
    thread_id: str | None = None,
) -> AsyncIterator[RunEvent]:
    """跑一轮主 Agent。

    Args:
        request: 用户这一轮的原始输入。
        agent: :func:`~evaharness.agents.lead_agent.agent.make_lead_agent` 编译好的图。
        run_id: 本次运行的标识；缺省自生。
        thread_id: **会话**标识（不是 checkpoint 坐标，坐标由 :func:`lead_thread_id`
            派生）；缺省自生。给了就用——前端因此能先斩后奏地开一段新会话。

    Yields:
        ``RunStartEvent`` → 若干 ``MessagesEvent`` → ``RunEndEvent``。

    Raises:
        不会为图级失败抛异常：它被翻成 ``RunEndEvent(status="failed")``，与
        Plan-Exec 把任务失败翻成 ``TaskEndEvent`` 是同一个 best-effort 立场。但
        :class:`asyncio.CancelledError` 继承 ``BaseException``，会**原样穿透**——
        客户端断开时的硬取消不该被伪装成一次「失败运行」（ADR-0005）。
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
    )

    coordinate = lead_thread_id(thread_id)
    payload = {"messages": [HumanMessage(content=request)]}
    last_state: Mapping[str, Any] | None = None
    try:
        async for kind, data in _astream(agent, payload, session_id=thread_id, coordinate=coordinate, run_id=run_id):
            if kind == "messages":
                chunk, metadata = data
                yield MessagesEvent(
                    task_id=LEAD_LANE,
                    chunk=chunk.model_dump(mode="json"),
                    metadata=metadata if isinstance(metadata, dict) else {},
                )
            else:
                last_state = data
    except Exception as exc:  # 非 CancelledError：模型错误已由中间件兜住，这里是图级失败的网
        yield RunEndEvent(status="failed", error=f"{type(exc).__name__}: {exc}")
        return
    yield RunEndEvent(status="completed", output=final_text(last_state))


def _astream(
    agent: CompiledStateGraph,
    payload: Mapping[str, Any],
    *,
    session_id: str,
    coordinate: str,
    run_id: str,
) -> AsyncIterator[tuple[str, Any]]:
    """统一多模式流式的载荷形状，便于 :func:`run_lead_agent` 保持扁平。"""
    return agent.astream(
        payload,
        config={"configurable": {"thread_id": coordinate}},
        context=LeadContext(session_id=session_id, thread_id=coordinate, run_id=run_id),
        stream_mode=["messages", "values"],
    )


__all__ = ["run_lead_agent"]
