"""契约测试：RunJournal 的可插拔 store（一个会话一个文件）。

两个实现（内存 / JSONL）必须能互换——这正是「可插拔」不是空话的证据，所以公共行为
用同一组断言跑两遍（见 ``store`` fixture）。
"""

import json

import pytest

from evaharness.agents.plan_exec.events import MessagesEvent, RunStartEvent, TaskEndEvent
from evaharness.journal.stores import (
    JournalEntry,
    JsonlJournalStore,
    MemoryJournalStore,
)

THREAD = "th-1"


def _start(seq: int, request: str = "现在几点？", *, run_id: str = "r-1", thread_id: str = THREAD) -> JournalEntry:
    return JournalEntry(
        seq=seq,
        event=RunStartEvent(
            run_id=run_id,
            thread_id=thread_id,
            request=request,
            started_at="2026-09-29T10:00:00+00:00",
        ),
    )


def _text(seq: int, text: str = "甲", *, task_id: str = "t1") -> JournalEntry:
    return JournalEntry(seq=seq, event=MessagesEvent(task_id=task_id, chunk={"content": text}))


def _end(seq: int) -> JournalEntry:
    return JournalEntry(seq=seq, event=TaskEndEvent(task_id="t1", status="done", output="完事"))


@pytest.fixture(params=["memory", "jsonl"])
def store(request, tmp_path):
    if request.param == "memory":
        return MemoryJournalStore()
    return JsonlJournalStore(tmp_path / "journal")


# --------------------------------------------------------------------------- #
# 两个实现的共同契约
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_append_then_load_roundtrips_in_order(store):
    await store.append(THREAD, [_start(1), _text(2, "甲"), _text(3, "乙")])

    entries = await store.load(THREAD)

    assert [e.seq for e in entries] == [1, 2, 3]
    assert [e.event.chunk["content"] for e in entries[1:]] == ["甲", "乙"]


@pytest.mark.asyncio
async def test_append_accumulates_across_calls(store):
    await store.append(THREAD, [_start(1)])
    await store.append(THREAD, [_text(2)])

    assert [e.seq for e in await store.load(THREAD)] == [1, 2]


@pytest.mark.asyncio
async def test_runs_of_one_thread_share_a_single_stream(store):
    """一个会话的多次运行写在同一条流里，行序就是跨运行的全序。"""
    await store.append(THREAD, [_start(1, "第一句", run_id="r-1"), _text(2, "甲")])
    await store.append(THREAD, [_start(3, "第二句", run_id="r-2"), _text(4, "乙")])

    entries = await store.load(THREAD)

    assert [e.event.type for e in entries] == ["run_start", "messages", "run_start", "messages"]
    assert [e.event.request for e in entries if e.event.type == "run_start"] == ["第一句", "第二句"]


@pytest.mark.asyncio
async def test_events_keep_their_type_through_the_roundtrip(store):
    """判别联合必须原样还原——回放要靠它分流到各自的分支。"""
    await store.append(THREAD, [_start(1), _end(2)])

    entries = await store.load(THREAD)

    assert [e.event.type for e in entries] == ["run_start", "task_end"]
    assert entries[1].event.output == "完事"


@pytest.mark.asyncio
async def test_threads_are_isolated_from_each_other(store):
    await store.append("th-1", [_text(1, "甲的")])
    await store.append("th-2", [_text(1, "乙的")])

    assert [e.event.chunk["content"] for e in await store.load("th-1")] == ["甲的"]
    assert [e.event.chunk["content"] for e in await store.load("th-2")] == ["乙的"]


@pytest.mark.asyncio
async def test_unknown_thread_loads_empty(store):
    assert await store.load("th-none") == []


@pytest.mark.asyncio
async def test_threads_lists_newest_first(store):
    await store.append("th-1", [_text(1)])
    await store.append("th-2", [_text(1)])

    assert [t.thread_id for t in await store.threads()] == ["th-2", "th-1"]


@pytest.mark.asyncio
async def test_empty_store_has_no_threads(store):
    assert await store.threads() == []


@pytest.mark.asyncio
async def test_thread_title_comes_from_the_first_run_request(store):
    """标题取会话**第一次运行**的用户请求——那正是「这个会话在聊什么」。"""
    await store.append(THREAD, [_start(1, "第一句问的是时间", run_id="r-1")])
    await store.append(THREAD, [_start(2, "第二句问的是天气", run_id="r-2")])

    [thread] = await store.threads()

    assert thread.title.startswith("第一句问的是时间")
    assert thread.run_count == 2
    assert thread.last_run_id == "r-2"


@pytest.mark.asyncio
async def test_preview_comes_from_the_latest_assistant_text(store):
    """摘要取最近一段助手正文——列表上「这个会话聊到哪了」。"""
    await store.append(THREAD, [_start(1, run_id="r-1"), _text(2, "旧的回答")])
    await store.append(THREAD, [_start(3, run_id="r-2"), _text(4, "最新的回答")])

    assert (await store.threads())[0].preview == "最新的回答"


@pytest.mark.asyncio
async def test_rename_overrides_the_derived_title(store):
    await store.append(THREAD, [_start(1, "原本的标题")])

    await store.rename_thread(THREAD, "我自己起的名字")

    assert (await store.threads())[0].title == "我自己起的名字"


@pytest.mark.asyncio
async def test_delete_thread_removes_everything(store):
    await store.append(THREAD, [_start(1)])
    await store.append(THREAD, [_start(2, run_id="r-2")])

    await store.delete_thread(THREAD)

    assert await store.threads() == []
    assert await store.load(THREAD) == []


@pytest.mark.asyncio
async def test_delete_is_scoped_to_one_thread(store):
    await store.append("th-1", [_text(1)])
    await store.append("th-2", [_text(1)])

    await store.delete_thread("th-1")

    assert [t.thread_id for t in await store.threads()] == ["th-2"]


@pytest.mark.asyncio
async def test_deleting_an_unknown_thread_is_a_noop(store):
    await store.delete_thread("th-none")  # 不抛即通过


# --------------------------------------------------------------------------- #
# JSONL 实现专有
# --------------------------------------------------------------------------- #


def _jsonl(tmp_path) -> JsonlJournalStore:
    return JsonlJournalStore(tmp_path / "journal")


@pytest.mark.asyncio
async def test_jsonl_layout_is_one_file_per_thread(tmp_path):
    store = _jsonl(tmp_path)

    await store.append("th-1", [_text(1)])
    await store.append("th-1", [_text(2)])  # 第二次运行也进同一个文件
    await store.append("th-2", [_text(1)])

    root = tmp_path / "journal"
    assert sorted(p.name for p in root.iterdir()) == ["th-1", "th-2"]
    assert (root / "th-1" / "journal.jsonl").is_file()


@pytest.mark.asyncio
async def test_jsonl_writes_one_line_per_entry(tmp_path):
    store = _jsonl(tmp_path)

    await store.append(THREAD, [_start(1), _text(2, "甲")])

    content = (tmp_path / "journal" / THREAD / "journal.jsonl").read_text("utf-8")
    lines = [line for line in content.splitlines() if line.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["seq"] == 1


@pytest.mark.asyncio
async def test_jsonl_creates_parent_directories(tmp_path):
    store = JsonlJournalStore(tmp_path / "深" / "几" / "层")

    await store.append(THREAD, [_text(1)])

    assert await store.load(THREAD) != []


@pytest.mark.asyncio
async def test_jsonl_skips_blank_and_truncated_lines(tmp_path):
    """硬杀进程会留下半截行。读的时候跳过它，而不是让整段会话炸掉。"""
    store = _jsonl(tmp_path)
    await store.append(THREAD, [_start(1), _text(2)])

    path = tmp_path / "journal" / THREAD / "journal.jsonl"
    path.write_text(path.read_text("utf-8") + "\n" + '{"seq": 3, "event": {"type": "mess', encoding="utf-8")

    assert [e.seq for e in await store.load(THREAD)] == [1, 2]


@pytest.mark.asyncio
async def test_jsonl_rename_is_survivable_by_a_fresh_store(tmp_path):
    """meta.json 落盘后，换一个 store 实例仍读得到自定义标题（跨进程有效）。"""
    await _jsonl(tmp_path).rename_thread(THREAD, "落盘的标题")
    await _jsonl(tmp_path).append(THREAD, [_start(1, "原始请求")])

    assert (await _jsonl(tmp_path).threads())[0].title == "落盘的标题"


@pytest.mark.asyncio
async def test_jsonl_falls_back_to_thread_id_when_nothing_is_derivable(tmp_path):
    store = _jsonl(tmp_path)
    await store.append(THREAD, [_text(1)])  # 没有 run_start，推不出标题

    assert (await store.threads())[0].title == THREAD


@pytest.mark.asyncio
async def test_jsonl_ignores_stray_files_and_directories_without_a_journal(tmp_path):
    store = _jsonl(tmp_path)
    await store.append("th-1", [_start(1)])

    root = tmp_path / "journal"
    (root / "README.txt").write_text("别把我当会话", encoding="utf-8")
    (root / "非法 目录").mkdir()
    (root / "空目录").mkdir()  # 没有 journal.jsonl
    (root / "t3-遗留").mkdir()
    (root / "t3-遗留" / "某次运行.jsonl").write_text("{}\n", encoding="utf-8")  # 旧布局，应当被忽略

    assert [t.thread_id for t in await store.threads()] == ["th-1"]


@pytest.mark.asyncio
async def test_jsonl_rejects_thread_ids_that_would_escape_the_directory(tmp_path):
    """thread_id 来自 URL 且会成为路径段——路径穿越必须在这里挡住。"""
    store = _jsonl(tmp_path)

    for evil in ["../../etc/passwd", "a/b", "..", "", "x" * 65]:
        with pytest.raises(ValueError):
            await store.load(evil)


@pytest.mark.asyncio
async def test_jsonl_deletes_the_whole_thread_directory(tmp_path):
    store = _jsonl(tmp_path)
    await store.append(THREAD, [_text(1)])

    await store.delete_thread(THREAD)

    assert not (tmp_path / "journal" / THREAD).exists()


@pytest.mark.asyncio
async def test_jsonl_accepts_str_path(tmp_path):
    store = JsonlJournalStore(str(tmp_path / "journal"))

    await store.append(THREAD, [_text(1)])

    assert [e.seq for e in await store.load(THREAD)] == [1]
