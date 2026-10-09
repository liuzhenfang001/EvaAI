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
lifespan 建好的那一张：中间件的追踪状态因此不跨请求。

**建图从 T13 起挪进了 run 生成器**（`_run_lead` / `_run_plan`）。原因是两个约束互相夹：
MCP 工具必须在 `create_agent(tools=...)` **之前**拿到手，而 MCP 连接必须活到**整轮跑完**。
所以「连 MCP → 建图 → 跑」只能发生在同一个异步作用域里，而那个作用域就是生成器体
（ADR-0011 已经定了「图的寿命 = 响应流的寿命」）。

**代价如实记着**：建图错误从「返回 StreamingResponse 之前炸 → JSON 500」变成「流里抛 →
SSE error 帧」（ADR-0011 记的那条后果，ADR-0017 更新）。这是有意的取舍：500 对前端本来就是
更难察觉的那种（它期望 SSE，收到 JSON 会表现为「没有任何反应」），而配置写错、类路径写错、
模型构造失败这几类已经被启动期 smoke build 挡在服务起来之前了。

请求**参数**的校验（`thread_id` 非法 → 400）与 `ensure_session_dirs` 仍然留在端点里、
生成器之外——那些该在流开始之前就给出 HTTP 状态码。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import aclosing
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.gateway.mcp import mcp_tools_for_run
from app.gateway.sse import sse_consumer
from app.tools.sandbox import ensure_session_dirs
from evaharness.agents.lead_agent import RunCancellation, run_lead_agent
from evaharness.agents.options import RunOptions, ThinkingLevel
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
    ensure_session_dirs(thread_id)
    run_id = uuid4().hex[:12]
    # 这一轮的运行参数：建图（模型）与 runner（事件回显）**必须拿同一份**——模型按 A 档跑、
    # 事件却报 B 档的话，回放与实时就分家了。
    options = RunOptions(thinking=body.thinking)
    return _stream(
        request,
        _run_lead(state, body, options, run_id, thread_id, _make_cancellation(state, run_id)),
        thread_id,
    )


async def _run_lead(
    state,
    body: ChatRequest,
    options: RunOptions,
    run_id: str,
    thread_id: str,
    cancellation: RunCancellation,
) -> AsyncIterator[RunEvent]:
    """一次运行的全部：进 MCP 连接 → 建图 → 跑 → 收摊。

    三层的 `aclosing` 里这是最里面一层，也是最要紧的一层：**它保证 MCP 连接会被还回去**。
    `async for` 提前退出**不会**自动关上游生成器，所以客户端断开（CancelledError 从
    `queue.get()` 穿透）时，包在外面的 `async with` 只能靠这里显式 `aclose` 才轮得到执行——
    不然 npx 子进程会一直留着（ADR-0017 记了这条实测）。

    连接与图**同生共死**：`agent` 是在连接开着的时候建的，所以它拿到的工具在整轮内都有效。
    """
    async with mcp_tools_for_run(state) as extra_tools:
        agent = state.agent_factory.lead(options, extra_tools=extra_tools)
        async with aclosing(
            run_lead_agent(
                body.content,
                agent=agent,
                run_id=run_id,
                thread_id=thread_id,
                cancellation=cancellation,
                options=options,
            )
        ) as events:
            async for event in events:
                yield event


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


@router.post("/plan/stream")
async def plan_stream(body: ChatRequest, request: Request) -> StreamingResponse:
    """Plan-Exec 封存路径：planner → 执行单元 → replanner → 汇总。

    三件套同样是每请求现建，且**必须一起换**——执行单元在装配期把图闭包进了 ``execute``，
    只换 planner / replanner 等于没换。
    """
    state = request.app.state
    thread_id = _resolve_thread_id(body)
    ensure_session_dirs(thread_id)
    run_id = uuid4().hex[:12]
    return _stream(
        request,
        _run_plan(state, body, RunOptions(thinking=body.thinking), run_id, thread_id),
        thread_id,
    )


async def _run_plan(
    state,
    body: ChatRequest,
    options: RunOptions,
    run_id: str,
    thread_id: str,
) -> AsyncIterator[RunEvent]:
    """封存路径的同构版本——**两套生命周期并存会让人误以为那是有理由的**（ADR-0011）。

    Plan-Exec 的取消不走 registry（T5 只给主路径做了硬取消），所以这里没有 `cancellation`。
    """
    async with mcp_tools_for_run(state) as extra_tools:
        roles = state.agent_factory.plan_exec(options, extra_tools=extra_tools)
        async with aclosing(
            run_plan_exec(
                body.content,
                planner=roles.planner,
                executor=roles.executor,
                replanner=roles.replanner,
                run_id=run_id,
                thread_id=thread_id,
            )
        ) as events:
            async for event in events:
                yield event


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
