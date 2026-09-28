"""SSE 帧格式化与流式生成器。

帧格式照搬 DeerFlow 的 ``format_sse``（`D:\\pythonPro\\deer-flow\\backend\\app\\gateway\\services.py`）：
``event:`` -> ``data:`` -> ``id:``(可选) -> 空行，``data`` 恒为 JSON。

流式来源为 ``graph.astream(stream_mode="messages")``（LangGraph Platform 的
``messages`` 事件，DeerFlow 公开名 ``messages-tuple``），逐 token 产出
``(AIMessageChunk, metadata)`` 元组；T1 无 StreamBridge/重连/gap/heartbeat，
直接转发为 ``messages`` 帧。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

from langgraph.graph.state import CompiledStateGraph


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


async def sse_consumer(graph: CompiledStateGraph, messages: Sequence[Any]) -> AsyncIterator[str]:
    """把 ``graph.astream(stream_mode="messages")`` 的流转成 SSE 帧序列。

    事件：``messages``（data=``[chunk_dict, metadata]``，token 增量）、``end``
    （data=null）、``error``（data=``{"message","name"}``，仅异常时）。
    """
    try:
        async for chunk, metadata in graph.astream(
            {"messages": list(messages)},
            stream_mode="messages",
        ):
            data = [chunk.model_dump(mode="json"), metadata if isinstance(metadata, dict) else {}]
            yield format_sse("messages", data)
        yield format_sse("end", None)
    except Exception as exc:  # 流式兜底：把异常转为 error 事件而非中断连接
        yield format_sse("error", {"message": str(exc), "name": type(exc).__name__})
        yield format_sse("end", None)
