"""chat 流式端点（SSE）。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from app.gateway.sse import sse_consumer

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    """chat 请求体（T1 最小：单条用户消息）。"""

    content: str


@router.post("/stream")
async def chat_stream(body: ChatRequest, request: Request) -> StreamingResponse:
    """创建一次无状态 chat 并流式返回 SSE。"""
    graph = request.app.state.agent
    messages = [HumanMessage(content=body.content)]
    return StreamingResponse(
        sse_consumer(graph, messages),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
