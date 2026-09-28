"""chat 流式端点（SSE）。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.gateway.sse import sse_consumer
from evaharness.agents.plan_exec import run_plan_exec

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    """chat 请求体：一条用户请求，由编排器拆成计划后执行。"""

    content: str


@router.post("/stream")
async def chat_stream(body: ChatRequest, request: Request) -> StreamingResponse:
    """跑一次 Plan-Exec 编排并以 SSE 流式返回全过程。"""
    state = request.app.state
    events = run_plan_exec(
        body.content,
        planner=state.planner,
        executor=state.executor,
        replanner=state.replanner,
    )
    return StreamingResponse(
        sse_consumer(events),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
