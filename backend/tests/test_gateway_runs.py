"""集成测试：运行记录端点（「回放」的服务端一半）。

最关键的一条是 :func:`test_replayed_journal_matches_the_live_stream`——它把「实时
看到的」与「回放拉回来的」摆在一起比对。回放若是另起一套实现，这条就会先炸。
"""

import json

import httpx
import pytest
from fastapi import FastAPI
from plan_exec_fakes import ExecutorStep, FakeExecutor, FakePlanner

from app.gateway.routers import runs
from app.gateway.sse import sse_consumer
from evaharness.agents.plan_exec import run_plan_exec
from evaharness.agents.plan_exec.events import PlanEvent
from evaharness.agents.plan_exec.schemas import Plan, PlanTask
from evaharness.journal import RunJournal, journaled
from evaharness.journal.stores import JsonlJournalStore, MemoryJournalStore


def _client(store) -> httpx.AsyncClient:
    app = FastAPI()
    app.state.journal_store = store
    app.include_router(runs.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _parse(frame):
    event = None
    data = None
    for line in frame.strip("\n").split("\n"):
        if line.startswith("event: "):
            event = line[len("event: ") :]
        elif line.startswith("data: "):
            data = json.loads(line[len("data: ") :])
    return event, data


# --------------------------------------------------------------------------- #
# 列表
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_runs_is_empty_before_anything_ran():
    async with _client(MemoryJournalStore()) as client:
        resp = await client.get("/runs")

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_a_recorded_run_appears_in_the_list():
    store = MemoryJournalStore()
    journal = RunJournal(store, "run-abc", flush_threshold=1)
    await journal.record(PlanEvent(run_id="run-abc", tasks=[]))

    async with _client(store) as client:
        resp = await client.get("/runs")

    assert [run["run_id"] for run in resp.json()] == ["run-abc"]
    assert resp.json()[0]["updated_at"]


# --------------------------------------------------------------------------- #
# 取回
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_journal_returns_every_entry_in_order():
    plan = Plan(tasks=[PlanTask(id="t1", description="查时间"), PlanTask(id="t2", description="算一算")])
    executor = FakeExecutor()

    async def events():
        async for event in run_plan_exec("req", planner=FakePlanner(plan), executor=executor, run_id="run-abc"):
            yield event

    store = MemoryJournalStore()
    frames = [frame async for frame in sse_consumer(journaled(events(), RunJournal(store, "run-abc")))]

    async with _client(store) as client:
        resp = await client.get("/runs/run-abc/journal")

    entries = resp.json()
    assert resp.status_code == 200
    assert [entry["seq"] for entry in entries] == list(range(1, len(entries) + 1))
    # `end` 帧是 SSE 层收尾用的，不是编排器产出的事件，所以不该进 journal。
    assert [entry["event"]["type"] for entry in entries] == [_parse(frame)[0] for frame in frames[:-1]]


@pytest.mark.asyncio
async def test_replayed_journal_matches_the_live_stream():
    """回放拉回来的东西必须与实时帧逐条对应——包括并行下的 task_id 归属。"""
    plan = Plan(tasks=[PlanTask(id="t1", description="甲"), PlanTask(id="t2", description="乙")])
    executor = FakeExecutor(
        {
            "t1": ExecutorStep(messages=("甲一", "甲二")),
            "t2": ExecutorStep(messages=("乙一", "乙二")),
        }
    )

    async def events():
        async for event in run_plan_exec("req", planner=FakePlanner(plan), executor=executor, run_id="run-abc"):
            yield event

    store = MemoryJournalStore()
    frames = [
        _parse(frame)
        async for frame in sse_consumer(journaled(events(), RunJournal(store, "run-abc", flush_threshold=3)))
    ]

    async with _client(store) as client:
        entries = (await client.get("/runs/run-abc/journal")).json()

    live = frames[:-1]  # 去掉 `end`
    # 比帧名而非 data：messages 帧的 data 是 [chunk, metadata] 数组，不是对象。
    assert [name for name, _ in live] == [entry["event"]["type"] for entry in entries]

    live_task_ids = [data[1]["task_id"] for name, data in live if name == "messages"]
    replayed_task_ids = [entry["event"]["task_id"] for entry in entries if entry["event"]["type"] == "messages"]
    assert live_task_ids == replayed_task_ids
    assert set(live_task_ids) == {"t1", "t2"}, "并行下两个任务的 token 都要留下归属"


@pytest.mark.asyncio
async def test_unknown_run_returns_404():
    async with _client(MemoryJournalStore()) as client:
        resp = await client.get("/runs/查无此run/journal")

    assert resp.status_code == 404
    assert "查无此run" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_illegal_run_id_returns_404_not_500(tmp_path):
    """非法 run_id 会被 store 拒绝；对调用方而言它与「不存在」没有区别，不该是 500。"""
    async with _client(JsonlJournalStore(tmp_path / "journal")) as client:
        resp = await client.get("/runs/坏.id/journal")

    assert resp.status_code == 404
