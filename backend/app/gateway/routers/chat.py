"""chat 流式端点（SSE）。

只有一条路径：``/chat/stream`` 跑 **ReAct 主 Agent**（T8 起的默认路径）。
曾经的 ``/chat/plan/stream``（Plan-Exec 封存路径）在 T16 下线了，见 ADR-0021——
它的**事件类型**还留在 :mod:`evaharness.agents.events` 里，只为解析老 journal。

这条路由在**请求路径上现建图**（`state.agent_factory`，见 ADR-0011），而不是共享
lifespan 建好的那一张：中间件的追踪状态因此不跨请求。

**建图在这里、`StreamingResponse` 之前**（ADR-0011 的取舍原样保留）：构造错误因此表现为 500，
而不是一条坏掉的流。

T13 一度把建图挪进了一个「一次运行」的异步生成器——那是为了在 MCP 连接的作用域里建图（连接要活到
整轮跑完）。T13 之后 MCP 改成了**启动期装载、调用时才连**（见 `app.gateway.mcp`），运行期没有
任何 MCP 资源要持有，那一层生成器就失去理由了，于是还原。

请求**参数**的校验（`thread_id` 非法 → 400）与会话文件空间的准备在更前面，理由不变：
那些该在流开始之前就给出 HTTP 状态码。

**上游生成器仍然关得干净**：`sse_consumer` 用 `aclosing` 关 `journaled`、`journaled` 用 `aclosing`
关 `run_lead_agent`——两层链一路到 runner 的 `finally`（取消 pump + 注销 run）。客户端断开时
`CancelledError` 从最深处穿透，链子照样收尾。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.gateway.sse import sse_consumer
from evaharness.agents.events import RunEvent
from evaharness.agents.lead_agent import RunCancellation, run_lead_agent
from evaharness.agents.options import RunOptions, ThinkingLevel
from evaharness.journal import RunJournal, journaled, validate_id

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    """chat 请求体。

    ``thread_id`` 缺省表示「开一段新会话」——服务端会生成一个，并随首个
    ``run_start`` 事件回传，所以前端不必先请求建会话。
    """

    content: str
    thread_id: str | None = None
    thinking: ThinkingLevel = "high"
    """思考档位（T12）。缺省 ``high`` 是**语义缺省**：新请求默认开思考。

    注意它和 ``RunStartEvent.thinking`` 的缺省（``off``）**故意不同**——那个是给老 journal 行
    用的历史回退（那些运行确实没思考）。两者各自的缺省都对着自己那一侧的事实。
    """


class CancelRequest(BaseModel):
    """取消请求体：按 ``run_id`` 触达一条正在跑的流（T5）。"""

    run_id: str


@router.post("/stream")
async def chat_stream(body: ChatRequest, request: Request) -> StreamingResponse:
    """跑一轮主 Agent 并以 SSE 流式返回。

    图是**这个请求专属**的：``state.agent_factory.lead()`` 现建一张（连带子 agent 图与
    模型），随这条流一起释放。请求之间因此不共享中间件实例——并发会话不会再互相写对方
    的循环检测账本与兜底重试预算（ADR-0011）。

    同一条事件流喂两个消费者：SSE 给前端实时看，RunJournal 落盘供事后回放
    （ADR-0006 定的「一个产出两个消费」）。``run_id`` 与 ``thread_id`` 在这里生成并
    同时交给两者，所以前端拿到的 id 一定能查到对应的记录。

    ``run_id`` 同时登记进 ``state.run_registry``（经 :class:`RunCancellation`），
    让 ``/chat/cancel`` 能在运行中触达这条流。
    """
    state = request.app.state
    thread_id = _resolve_thread_id(body)
    # 会话一诞生就有它的文件空间：用户能在对话前把文件放进 /input，而「有没有文件空间」
    # 不取决于模型想没想起来调工具。必须在建图与开跑之前。
    state.sandbox_layout.ensure_session_dirs(thread_id)
    run_id = uuid4().hex[:12]
    # 这一轮的运行参数：建图（模型）与 runner（事件回显）**必须拿同一份**——模型按 A 档跑、
    # 事件却报 B 档的话，回放与实时就分家了。
    options = RunOptions(thinking=body.thinking)
    return _stream(
        request,
        run_lead_agent(
            body.content,
            agent=state.agent_factory.lead(options),
            run_id=run_id,
            thread_id=thread_id,
            cancellation=_make_cancellation(state, run_id),
            options=options,
            # 布局随这次运行下发：工具侧从运行上下文取（见 ADR-0020），不读任何全局。
            sandbox=state.sandbox_layout,
        ),
        thread_id,
    )


@router.post("/cancel")
async def cancel_run(body: CancelRequest, request: Request) -> dict[str, bool]:
    """取消一条正在跑的流。

    命中且仍在跑返回 ``{"cancelled": true}``；未命中（run 不存在、或已自然结束）返回
    404——取消一个已经结束的 run 不是错，但「我们确实没取消任何东西」该让前端知道。
    """
    registry = getattr(request.app.state, "run_registry", None)
    if registry is None or not registry.cancel(body.run_id):
        raise HTTPException(status_code=404, detail="没有正在运行的 run")
    return {"cancelled": True}


def _make_cancellation(state, run_id: str) -> RunCancellation:
    """把 registry + run_id 绑成 runner 要的取消接缝；没有 registry 就退化成 no-op。"""
    registry = getattr(state, "run_registry", None)
    if registry is None:
        return RunCancellation()
    return RunCancellation(
        register=lambda task: registry.register(run_id, task),
        unregister=lambda: registry.unregister(run_id),
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
