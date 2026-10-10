"""RunEvent → SSE 帧的映射与流式生成器。

帧格式照搬 DeerFlow 的 ``format_sse``（`D:\\pythonPro\\deer-flow\\backend\\app\\gateway\\services.py`）：
``event:`` -> ``data:`` -> ``id:``(可选) -> 空行，``data`` 恒为 JSON。

这一层只做传输，不做编排：事件由 ``evaharness`` 产出，这里按事件名逐个转帧。
``messages`` 帧沿用 T1 的线上形状（``data = [chunk, metadata]``），其余事件是
「带 ``type`` 判别键的对象」——两套形状并存是有意的，后者能承载结构化载荷。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from evaharness.agents.events import MessagesEvent, RunEvent


def format_sse(event: str, data: Any, *, event_id: str | None = None) -> str:
    """格式化单个 SSE 帧。

    字段顺序：``event:`` -> ``data:`` -> ``id:``(可选) -> 空行，帧尾为 ``\\n\\n``。
    ``data`` 恒为 JSON（``ensure_ascii=False`` 保留中文，``default=str`` 兜底非 JSON 类型）。
    """
    payload = json.dumps(data, default=str, ensure_ascii=False)
    parts = [f"event: {event}", f"data: {payload}"]
    if event_id:
        parts.append(f"id: {event_id}")
    parts.append("")
    parts.append("")
    return "\n".join(parts)


def map_run_event(event: RunEvent) -> str:
    """把一个 :class:`RunEvent` 转成 SSE 帧。

    ``MessagesEvent`` 的 ``task_id`` 并进 ``metadata`` 而不是新增数组元素：langgraph
    本来就把「这条 chunk 的附属信息」放在 metadata 里，加一个自有键是同一精神，
    而 T1 定下的 ``data = [chunk, metadata]`` 形状一字未动。并行派发后 token 会交错
    到达，前端靠这个键把增量分流到各自的气泡与时间线泳道。
    """
    if isinstance(event, MessagesEvent):
        return format_sse("messages", [event.chunk, {**event.metadata, "task_id": event.task_id}])
    return format_sse(event.type, event.model_dump(mode="json"))


async def sse_consumer(events: AsyncIterator[RunEvent]) -> AsyncIterator[str]:
    """把 RunEvent 流转成 SSE 帧序列。

    事件名直接取自事件的 ``type``；流以 ``end`` 收尾，异常转 ``error`` 帧。

    注意 ``except Exception`` 拦不到 :class:`asyncio.CancelledError`（它继承
    ``BaseException``）——客户端断开时的硬取消会原样穿透（ADR-0005）。

    **`aclosing` 不是可选的**（T13 补上）：`async for` 提前退出**不会**关上游生成器，
    所以断开时上游那些包在生成器里的 ``AsyncExitStack``（MCP 连接就在那里）**永远不会被退出**
    ——npx 子进程会一直留着。套一层 `aclosing` 就把「关上游」锚在异常解开的路径上，不再依赖
    框架来调 `aclose`（Starlette 不保证会调）。
    """
    try:
        async with aclosing(events) as stream:
            async for event in stream:
                yield map_run_event(event)
        yield format_sse("end", None)
    except Exception as exc:  # 流式兜底：把异常转为 error 事件而非中断连接
        yield format_sse("error", {"message": str(exc), "name": type(exc).__name__})
        yield format_sse("end", None)
