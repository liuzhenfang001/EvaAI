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

两条路由都在**请求路径上现建图**（`state.agent_factory`，见 ADR-0011），而不是共享
lifespan 建好的那一张：中间件的追踪状态因此不跨请求。建图在返回 `StreamingResponse`
**之前**发生，所以构造错误表现为 500，而不是一条坏掉的流。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.gateway.sse import sse_consumer
from app.tools.sandbox import ensure_session_dirs
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

    图是**这个请求专属**的：``state.agent_factory.lead()`` 现建一张（连带子 agent 图与
    模型），随这条流一起释放。请求之间因此不共享中间件实例——并发会话不会再互相写对方
    的循环检测账本与兜底重试预算（ADR-0011）。

    同一条事件流喂两个消费者：SSE 给前端实时看，RunJournal 落盘供事后回放
    （ADR-0006 定的「一个产出两个消费」）。``run_id`` 与 ``thread_id`` 在这里生成并
    同时交给两者，所以前端拿到的 id 一定能查到对应的记录。
    """
    state = request.app.state
    thread_id = _resolve_thread_id(body)
    # 会话一诞生就有它的文件空间：用户能在对话前把文件放进 /input，而「有没有文件空间」
    # 不取决于模型想没想起来调工具。必须在建图与开跑之前。
    ensure_session_dirs(thread_id)
    return _stream(
        request,
        run_lead_agent(
            body.content,
            agent=state.agent_factory.lead(),
            run_id=uuid4().hex[:12],
            thread_id=thread_id,
        ),
        thread_id,
    )


@router.post("/plan/stream")
async def plan_stream(body: ChatRequest, request: Request) -> StreamingResponse:
    """Plan-Exec 封存路径：planner → 执行单元 → replanner → 汇总。

    三件套同样是每请求现建，且**必须一起换**——执行单元在装配期把图闭包进了 ``execute``，
    只换 planner / replanner 等于没换。
    """
    state = request.app.state
    thread_id = _resolve_thread_id(body)
    ensure_session_dirs(thread_id)
    roles = state.agent_factory.plan_exec()
    return _stream(
        request,
        run_plan_exec(
            body.content,
            planner=roles.planner,
            executor=roles.executor,
            replanner=roles.replanner,
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
