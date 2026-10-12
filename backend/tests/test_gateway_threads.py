"""集成测试：会话端点（「回放」的服务端一半）。

最关键的一条是 :func:`test_replayed_messages_match_the_live_stream`——它把「实时
看到的」与「回放拉回来的」摆在一起比对：回放若是另起一套实现，这条会先炸。

事件源是**真的**：一次主 Agent 编排 + 一个假模型（T16 之前借的是 Plan-Exec 的假
planner/executor，那条路径已下线，见 ADR-0021）。
"""

import itertools
import json
from pathlib import Path

import httpx
import pytest
from fakes import ToolCapableFake
from fastapi import FastAPI
from langchain_core.messages import AIMessage

from app.gateway.routers import threads
from app.gateway.sse import sse_consumer
from evaharness.agents.lead_agent import make_lead_agent
from evaharness.journal import RunJournal, journaled
from evaharness.journal.stores import JsonlJournalStore, MemoryJournalStore
from evaharness.runtime import DEFAULT_LANE, run_agent
from evaharness.sandbox import SandboxLayout

THREAD = "th-1"


def _client(store, *, bash_enabled: bool = False, sandbox_layout=None) -> httpx.AsyncClient:
    app = FastAPI()
    app.state.journal_store = store
    app.state.bash_enabled = bash_enabled
    # 删容器要按 deployment label 找，而那个 label 由布局的宿主根算出——所以路由需要它。
    app.state.sandbox_layout = sandbox_layout or SandboxLayout(root=Path("C:/evaai-unused"))
    app.include_router(threads.router)
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


async def _run(store, request: str, *, thread_id: str = THREAD, run_id: str = "r-1", reply: str = "现在是 18:04"):
    """跑一次真实编排（主 Agent + 假模型）并把事件流同时喂给 store 与 SSE。

    ``reply`` 是这次回复的正文。假模型（``GenericFakeChatModel``）按**空白**切块流式输出，
    所以带空格的一句会产出多条 ``messages`` 增量——归并与回放那几条测试要的就是这个
    （原先靠 Plan-Exec 的执行单元替身喂出多条增量，T16 下线后改走这条真路径）。
    """
    model = ToolCapableFake(messages=itertools.cycle([AIMessage(content=reply)]))
    agent = make_lead_agent(model, [], system_prompt="你是助手")

    async def events():
        async for event in run_agent(request, agent=agent, thread_id=thread_id, run_id=run_id):
            yield event

    journal = RunJournal(store, thread_id)
    frames = [_parse(frame) async for frame in sse_consumer(journaled(events(), journal))]
    return frames


# --------------------------------------------------------------------------- #
# 列表
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_threads_is_empty_before_anything_ran():
    async with _client(MemoryJournalStore()) as client:
        resp = await client.get("/threads")

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_a_recorded_run_appears_with_a_title_derived_from_the_request():
    store = MemoryJournalStore()
    await _run(store, "现在几点？")

    async with _client(store) as client:
        resp = await client.get("/threads")

    [thread] = resp.json()
    assert thread["thread_id"] == THREAD
    assert thread["title"].startswith("现在几点")
    assert thread["run_count"] == 1
    assert thread["last_run_id"] == "r-1"


@pytest.mark.asyncio
async def test_two_runs_in_one_thread_are_counted_and_the_last_is_reported():
    store = MemoryJournalStore()
    await _run(store, "第一句", run_id="r-1")
    await _run(store, "第二句", run_id="r-2")

    async with _client(store) as client:
        [thread] = (await client.get("/threads")).json()

    assert thread["run_count"] == 2
    assert thread["last_run_id"] == "r-2"
    assert thread["title"].startswith("第一句"), "标题取会话的**第一句**，不是最近一句"


# --------------------------------------------------------------------------- #
# 对话投影
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_messages_projection_returns_only_the_message_bucket():
    store = MemoryJournalStore()
    await _run(store, "现在几点？", reply="现 在 18:04")

    async with _client(store) as client:
        entries = (await client.get(f"/threads/{THREAD}/messages")).json()

    types = [entry["event"]["type"] for entry in entries]
    assert types[0] == "run_start", "用户的请求是对话的第一条"
    assert "plan" not in types and "task_start" not in types and "summary" not in types
    assert set(types) <= {"run_start", "messages", "run_end"}


@pytest.mark.asyncio
async def test_messages_projection_reflects_compaction():
    """落盘已归并，读路径不必再加工——三条增量变一条。"""
    store = MemoryJournalStore()
    await _run(store, "现在几点？", reply="现 在 18:04")

    async with _client(store) as client:
        entries = (await client.get(f"/threads/{THREAD}/messages")).json()

    texts = [e["event"]["chunk"]["content"] for e in entries if e["event"]["type"] == "messages"]
    assert texts == ["现 在 18:04"]


@pytest.mark.asyncio
async def test_messages_projection_spans_runs_oldest_first():
    store = MemoryJournalStore()
    await _run(store, "第一句", run_id="r-1", reply="甲")
    await _run(store, "第二句", run_id="r-2", reply="乙")

    async with _client(store) as client:
        entries = (await client.get(f"/threads/{THREAD}/messages")).json()

    requests = [e["event"]["request"] for e in entries if e["event"]["type"] == "run_start"]
    assert requests == ["第一句", "第二句"], "对话必须由早到晚，不能顺着 runs() 的最近优先"


@pytest.mark.asyncio
async def test_replayed_messages_match_the_live_stream():
    """回放拉回来的对话必须与实时帧逐字一致（归并是唯一允许的差异）。"""
    store = MemoryJournalStore()
    frames = await _run(store, "现在几点？", reply="现 在 18:04")

    async with _client(store) as client:
        entries = (await client.get(f"/threads/{THREAD}/messages")).json()

    live = [data for name, data in frames if name == "messages"]
    replayed = [e["event"] for e in entries if e["event"]["type"] == "messages"]

    assert _text_per_task(live) == _text_per_task(replayed)
    assert len(replayed) < len(live), "落盘已归并——回放拉回的条目该比实时帧少"

    live_requests = [data["request"] for name, data in frames if name == "run_start"]
    assert [e["event"]["request"] for e in entries if e["event"]["type"] == "run_start"] == live_requests


def _text_per_task(events) -> dict[str, str]:
    """每个任务的正文按序拼接——前端气泡的可观测内容。

    要同时吃两种形状：SSE 的 ``messages`` 帧是 ``[chunk, metadata]``，而 journal 里
    是自描述对象（见 sse.py 的说明）。两边都要能算，否则比不出「历史与实时一致」。
    """
    texts: dict[str, str] = {}
    for raw in events:
        if isinstance(raw, list):
            chunk, metadata = raw
            task_id = metadata.get("task_id")
        else:
            chunk, task_id = raw["chunk"], raw["task_id"]
        content = chunk.get("content")
        if isinstance(content, str) and content and chunk.get("type") != "tool":
            texts[task_id] = texts.get(task_id, "") + content
    return texts


# --------------------------------------------------------------------------- #
# trace 投影
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_thread_journal_returns_the_full_trace():
    """面板要的是**全部**事件——它得靠 messages 里的工具行与正文才能重建。"""
    store = MemoryJournalStore()
    await _run(store, "现在几点？", reply="甲")

    async with _client(store) as client:
        entries = (await client.get(f"/threads/{THREAD}/journal")).json()

    types = [entry["event"]["type"] for entry in entries]
    assert types == ["run_start", "messages", "run_end"], "主 Agent 不拆任务：没有 plan / task_* / summary"


@pytest.mark.asyncio
async def test_trace_projection_is_a_superset_of_the_message_projection():
    store = MemoryJournalStore()
    await _run(store, "现在几点？", reply="甲")

    async with _client(store) as client:
        trace = (await client.get(f"/threads/{THREAD}/journal")).json()
        messages = (await client.get(f"/threads/{THREAD}/messages")).json()

    assert {e["event"]["type"] for e in messages} <= {e["event"]["type"] for e in trace}


# --------------------------------------------------------------------------- #
# 重命名 / 删除
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rename_changes_the_title():
    store = MemoryJournalStore()
    await _run(store, "原始标题")

    async with _client(store) as client:
        resp = await client.patch(f"/threads/{THREAD}", json={"title": "我自己起的"})
        [thread] = (await client.get("/threads")).json()

    assert resp.status_code == 200
    assert resp.json()["title"] == "我自己起的"
    assert thread["title"] == "我自己起的"


@pytest.mark.asyncio
async def test_rename_of_an_unknown_thread_returns_404():
    async with _client(MemoryJournalStore()) as client:
        resp = await client.patch("/threads/nope", json={"title": "x"})

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_removes_the_thread():
    store = MemoryJournalStore()
    await _run(store, "要删掉的")

    async with _client(store) as client:
        resp = await client.delete(f"/threads/{THREAD}")
        listed = (await client.get("/threads")).json()
        after = await client.get(f"/threads/{THREAD}/messages")

    assert resp.status_code == 204
    assert listed == []
    assert after.status_code == 404


@pytest.mark.asyncio
async def test_delete_is_idempotent():
    """前端删完会刷新列表，竞态下重复删不该报错。"""
    async with _client(MemoryJournalStore()) as client:
        assert (await client.delete("/threads/nope")).status_code == 204


# --------------------------------------------------------------------------- #
# 连带删掉沙箱容器（T15）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_delete_also_removes_the_sessions_sandbox_container(monkeypatch):
    """容器是这个会话的**运行状态**——会话没了，它就该跟着走。"""
    seen: list[str] = []
    store = MemoryJournalStore()
    await _run(store, "要删掉的")
    monkeypatch.setattr(threads.docker, "remove_session_container", lambda _layout, session: seen.append(session))

    async with _client(store, bash_enabled=True) as client:
        assert (await client.delete(f"/threads/{THREAD}")).status_code == 204

    assert seen == [THREAD]


@pytest.mark.asyncio
async def test_a_failing_container_removal_still_returns_204(monkeypatch):
    """收尾失败不该让调用方以为没删掉——会话本身确实已经没了。"""

    def boom(_layout, _session):  # noqa: ANN001
        raise RuntimeError("docker 不见了")

    store = MemoryJournalStore()
    await _run(store, "要删掉的")
    monkeypatch.setattr(threads.docker, "remove_session_container", boom)

    async with _client(store, bash_enabled=True) as client:
        assert (await client.delete(f"/threads/{THREAD}")).status_code == 204


@pytest.mark.asyncio
async def test_delete_never_touches_docker_without_bash(monkeypatch):
    """没启用 bash 的部署没有容器可删，一个 docker 命令都不该发。"""

    def boom(_layout, _session):  # noqa: ANN001
        raise AssertionError("没启用 bash 却碰了 docker")

    store = MemoryJournalStore()
    await _run(store, "要删掉的")
    monkeypatch.setattr(threads.docker, "remove_session_container", boom)

    async with _client(store) as client:
        assert (await client.delete(f"/threads/{THREAD}")).status_code == 204


# --------------------------------------------------------------------------- #
# 错误路径
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["/threads/nope/messages", "/threads/nope/journal"],
)
async def test_unknown_thread_returns_404(path):
    async with _client(MemoryJournalStore()) as client:
        assert (await client.get(path)).status_code == 404


@pytest.mark.asyncio
async def test_illegal_thread_id_returns_404_not_500(tmp_path):
    """非法 id 会被 store 拒绝；对调用方而言它与「不存在」没有区别，不该是 500。"""
    async with _client(JsonlJournalStore(tmp_path / "journal")) as client:
        assert (await client.get("/threads/坏.id/messages")).status_code == 404


@pytest.mark.asyncio
async def test_thread_journal_concatenates_runs_oldest_first():
    """一次拉全整段会话——前端就靠这一份事件流重建对话与面板。"""
    store = MemoryJournalStore()
    await _run(store, "第一句", run_id="r-1", reply="甲")
    await _run(store, "第二句", run_id="r-2", reply="乙")

    async with _client(store) as client:
        entries = (await client.get(f"/threads/{THREAD}/journal")).json()

    requests = [e["event"]["request"] for e in entries if e["event"]["type"] == "run_start"]
    assert requests == ["第一句", "第二句"]


@pytest.mark.asyncio
async def test_preview_summarizes_the_latest_assistant_text():
    """列表上「这个会话聊到哪了」——取的是 message 桶里的最后一段正文。"""
    store = MemoryJournalStore()
    await _run(store, "第一句", run_id="r-1", reply="旧")
    await _run(store, "第二句", run_id="r-2", reply="最新")

    async with _client(store) as client:
        [thread] = (await client.get("/threads")).json()

    assert thread["preview"] == "最新"


@pytest.mark.asyncio
async def test_jsonl_store_survives_a_fresh_client(tmp_path):
    """换一个 store 实例仍读得到——这条是「重启后端后对话还在」的服务端证据。"""
    store = JsonlJournalStore(tmp_path / "journal")
    await _run(store, "重启前问的", reply="甲")

    async with _client(JsonlJournalStore(tmp_path / "journal")) as client:
        threads_payload = (await client.get("/threads")).json()
        entries = (await client.get(f"/threads/{THREAD}/messages")).json()

    assert threads_payload[0]["title"].startswith("重启前问的")
    assert [e["event"]["type"] for e in entries] == ["run_start", "messages", "run_end"]
    assert _text_per_task([e["event"] for e in entries if e["event"]["type"] == "messages"]) == {DEFAULT_LANE: "甲"}
