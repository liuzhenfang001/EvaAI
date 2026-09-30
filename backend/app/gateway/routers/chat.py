"""chat 流式端点（SSE）。"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.gateway.sse import sse_consumer
from evaharness.agents.plan_exec import run_plan_exec
from evaharness.journal import RunJournal, journaled, validate_id

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    """chat 请求体。

    ``thread_id`` 缺省表示「开一段新会话」——服务端会生成一个，并随首个
    ``run_start`` 事件回传，所以前端不必先请求建会话。
    """

    content: str
    thread_id: str | None = None


@router.post("/stream")
async def chat_stream(body: ChatRequest, request: Request) -> StreamingResponse:
    """跑一次 Plan-Exec 编排并以 SSE 流式返回全过程。

    同一条事件流喂两个消费者：SSE 给前端实时看，RunJournal 落盘供事后回放
    （ADR-0006 定的「一个产出两个消费」）。``run_id`` 与 ``thread_id`` 在这里生成并
    同时交给两者，所以前端拿到的 id 一定能查到对应的记录。
    """
    state = request.app.state
    thread_id = body.thread_id or uuid4().hex[:12]
    try:
        # thread_id 会成为 journal 的路径段，先在这里挡掉非法输入（400 而非 500）。
        validate_id(thread_id, kind="thread_id")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    run_id = uuid4().hex[:12]
    journal = RunJournal(
        state.journal_store,
        thread_id,
        flush_threshold=state.journal_config.flush_threshold,
    )
    events = run_plan_exec(
        body.content,
        planner=state.planner,
        executor=state.executor,
        replanner=state.replanner,
        run_id=run_id,
        thread_id=thread_id,
    )
    return StreamingResponse(
        sse_consumer(journaled(events, journal)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
