"""chat 流式端点（SSE）。

两条路径，同一套外壳：

===============  ==========================  ===================================
路径              跑什么                       说明
===============  ==========================  ===================================
``/chat/stream``  **ReAct 主 Agent**（默认）     T8 起的默认路径
``/chat/plan/stream``  Plan-Exec 编排           封存；路由与测试都保留，没有 UI 入口
===============  ==========================  ===================================

封存不等于放弃——Plan-Exec 将来要以「主 Agent 的一个子能力」回来（见 ADR-0010），
所以它的路由与测试原样留着，只是不再是默认。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.gateway.sse import sse_consumer
from evaharness.agents.lead_agent import run_lead_agent
from evaharness.agents.plan_exec import run_plan_exec
from evaharness.agents.plan_exec.events import RunEvent
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
    """跑一轮主 Agent 并以 SSE 流式返回。

    同一条事件流喂两个消费者：SSE 给前端实时看，RunJournal 落盘供事后回放
    （ADR-0006 定的「一个产出两个消费」）。``run_id`` 与 ``thread_id`` 在这里生成并
    同时交给两者，所以前端拿到的 id 一定能查到对应的记录。
    """
    state = request.app.state
    thread_id = _resolve_thread_id(body)
    return _stream(
        request,
        run_lead_agent(
            body.content,
            agent=state.lead_agent,
            run_id=uuid4().hex[:12],
            thread_id=thread_id,
        ),
        thread_id,
    )


@router.post("/plan/stream")
async def plan_stream(body: ChatRequest, request: Request) -> StreamingResponse:
    """Plan-Exec 封存路径：planner → 执行单元 → replanner → 汇总。"""
    state = request.app.state
    thread_id = _resolve_thread_id(body)
    return _stream(
        request,
        run_plan_exec(
            body.content,
            planner=state.planner,
            executor=state.executor,
            replanner=state.replanner,
            run_id=uuid4().hex[:12],
            thread_id=thread_id,
        ),
        thread_id,
    )


def _resolve_thread_id(body: ChatRequest) -> str:
    """取（必要时生成）会话 id，并挡掉非法输入。

    ``thread_id`` 会成为 journal 的路径段，所以在这里挡掉——回 400 而不是让 store
    在写入时炸成 500。
    """
    thread_id = body.thread_id or uuid4().hex[:12]
    try:
        validate_id(thread_id, kind="thread_id")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return thread_id


def _stream(request: Request, events: AsyncIterator[RunEvent], thread_id: str) -> StreamingResponse:
    """把一条 RunEvent 流包成 SSE 响应，并顺手记账到 journal。"""
    state = request.app.state
    journal = RunJournal(
        state.journal_store,
        thread_id,
        flush_threshold=state.journal_config.flush_threshold,
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
