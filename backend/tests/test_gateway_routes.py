"""两条 chat 路由的接线：谁跑什么、首尾帧是什么、有没有记账。

``test_gateway_chat.py`` 直接把生成器喂给 ``sse_consumer``，绕过了 FastAPI 这一层；
这里补上被绕过的那一段——**接错线是这一类改动最容易犯又最难发现的错**（比如默认
路由还指着封存的 Plan-Exec），而且它不会让任何单元测试变红。
"""

import json

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from plan_exec_fakes import FakePlanner, ToolCapableFake

from app.gateway.routers import chat
from evaharness.agents.lead_agent import LEAD_LANE, make_lead_agent
from evaharness.agents.plan_exec import make_react_executor, make_replanner
from evaharness.agents.plan_exec.schemas import Plan, PlanTask
from evaharness.journal.stores import MemoryJournalStore

THREAD = "th-1"


def _parse(frame: str):
    event = None
    data = None
    for line in frame.strip("\n").split("\n"):
        if line.startswith("event: "):
            event = line[len("event: ") :]
        elif line.startswith("data: "):
            data = json.loads(line[len("data: ") :])
    return event, data


def _client(store: MemoryJournalStore, app_config=None) -> httpx.AsyncClient:
    """把 chat 路由挂在一个状态齐备的裸 app 上（不起 lifespan、不读配置文件）。"""
    model = ToolCapableFake(messages=iter([AIMessage(content="现在是 18:04")]))
    app = FastAPI()
    app.state.journal_store = store
    app.state.journal_config = app_config
    app.state.lead_agent = make_lead_agent(model, [], checkpointer=InMemorySaver())
    app.state.planner = FakePlanner(Plan(tasks=[PlanTask(id="t1", description="查时间")]))
    app.state.executor = make_react_executor(
        ToolCapableFake(messages=iter([AIMessage(content="现在是 18:04")])),
        checkpointer=InMemorySaver(),
    )
    app.state.replanner = make_replanner(ToolCapableFake(messages=iter([AIMessage(content="")])))
    app.include_router(chat.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class _JournalConfig:
    flush_threshold = 20


async def _post(client: httpx.AsyncClient, path: str, content: str, **extra):
    resp = await client.post(path, json={"content": content, "thread_id": THREAD, **extra})
    assert resp.status_code == 200, resp.text
    return [_parse(frame) for frame in resp.text.split("\n\n") if frame.strip()]


# --------------------------------------------------------------------------- #
# 默认路径：主 Agent
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_default_route_runs_the_lead_agent():
    """``/chat/stream`` 必须是主 Agent——它是「默认 ReAct 直答」这句话的唯一落点。"""
    async with _client(MemoryJournalStore(), _JournalConfig()) as client:
        frames = await _post(client, "/chat/stream", "现在几点？")

    names = [name for name, _ in frames]
    assert names[0] == "run_start"
    assert names[-1] == "end"
    assert "plan" not in names, "主 Agent 不该产出计划"
    assert "task_start" not in names
    assert "run_end" in names


@pytest.mark.asyncio
async def test_the_default_route_declares_react_mode():
    async with _client(MemoryJournalStore(), _JournalConfig()) as client:
        frames = await _post(client, "/chat/stream", "现在几点？")

    _, data = frames[0]
    assert data["mode"] == "react"
    assert data["thread_id"] == THREAD


@pytest.mark.asyncio
async def test_the_default_route_uses_a_non_empty_lane():
    """空泳道 id 会让前端静默丢弃全部正文（``ensureBubble`` 对空值 no-op）。"""
    async with _client(MemoryJournalStore(), _JournalConfig()) as client:
        frames = await _post(client, "/chat/stream", "现在几点？")

    lanes = {data[1]["task_id"] for name, data in frames if name == "messages"}
    assert lanes == {LEAD_LANE}


@pytest.mark.asyncio
async def test_the_default_route_records_the_run():
    store = MemoryJournalStore()
    async with _client(store, _JournalConfig()) as client:
        await _post(client, "/chat/stream", "现在几点？")

    types = [entry.event.type for entry in await store.load(THREAD)]
    assert types[0] == "run_start"
    assert types[-1] == "run_end"


# --------------------------------------------------------------------------- #
# 封存路径：Plan-Exec
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_plan_route_still_runs_plan_exec():
    """封存不等于坏掉：路由还在，且仍然产出它那套事件。"""
    async with _client(MemoryJournalStore(), _JournalConfig()) as client:
        frames = await _post(client, "/chat/plan/stream", "现在几点？")

    names = [name for name, _ in frames]
    assert names[0] == "run_start"
    assert "plan" in names
    assert "task_start" in names
    assert "summary" in names
    assert names[-1] == "end"


@pytest.mark.asyncio
async def test_the_plan_route_declares_plan_mode():
    async with _client(MemoryJournalStore(), _JournalConfig()) as client:
        frames = await _post(client, "/chat/plan/stream", "现在几点？")

    _, data = frames[0]
    assert data["mode"] == "plan"


# --------------------------------------------------------------------------- #
# 参数校验（两条路径共用一套外壳）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_illegal_thread_id_is_rejected_before_the_run():
    """非法 id 会是 journal 的路径段，必须在入口挡成 400 而不是写盘时炸成 500。"""
    async with _client(MemoryJournalStore(), _JournalConfig()) as client:
        for path in ("/chat/stream", "/chat/plan/stream"):
            resp = await client.post(path, json={"content": "你好", "thread_id": "坏.id"})
            assert resp.status_code == 400, path


@pytest.mark.asyncio
async def test_a_generated_thread_id_comes_back_in_run_start():
    """不给 id 时服务端生成，并随首个事件回传——前端因此不必先建会话。"""
    async with _client(MemoryJournalStore(), _JournalConfig()) as client:
        resp = await client.post("/chat/stream", json={"content": "你好"})

    _, data = _parse(resp.text.split("\n\n")[0])
    assert data["thread_id"]
