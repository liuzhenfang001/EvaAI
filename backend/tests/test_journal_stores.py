"""契约测试：RunJournal 的可插拔 store。

两个实现（内存 / JSONL）必须能互换——这正是「可插拔」不是空话的证据，所以
公共行为用同一组断言跑两遍（见文件末尾的参数化）。
"""

import json

import pytest

from evaharness.agents.plan_exec.events import MessagesEvent, TaskEndEvent
from evaharness.journal.stores import (
    JournalEntry,
    JsonlJournalStore,
    MemoryJournalStore,
)


def _entry(seq: int, text: str = "x") -> JournalEntry:
    return JournalEntry(seq=seq, event=MessagesEvent(task_id="t1", chunk={"content": text}))


def _end_entry(seq: int) -> JournalEntry:
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
    await store.append("run-1", [_entry(0, "甲"), _entry(1, "乙")])

    entries = await store.load("run-1")

    assert [e.seq for e in entries] == [0, 1]
    assert [e.event.chunk["content"] for e in entries] == ["甲", "乙"]


@pytest.mark.asyncio
async def test_append_accumulates_across_calls(store):
    await store.append("run-1", [_entry(0)])
    await store.append("run-1", [_entry(1)])

    assert [e.seq for e in await store.load("run-1")] == [0, 1]


@pytest.mark.asyncio
async def test_events_keep_their_type_through_the_roundtrip(store):
    """判别联合必须原样还原——回放要靠它分流到各自的分支。"""
    await store.append("run-1", [_entry(0), _end_entry(1)])

    entries = await store.load("run-1")

    assert [e.event.type for e in entries] == ["messages", "task_end"]
    assert entries[1].event.status == "done"
    assert entries[1].event.output == "完事"


@pytest.mark.asyncio
async def test_runs_are_isolated_from_each_other(store):
    await store.append("run-1", [_entry(0, "甲的")])
    await store.append("run-2", [_entry(0, "乙的")])

    assert [e.event.chunk["content"] for e in await store.load("run-1")] == ["甲的"]
    assert [e.event.chunk["content"] for e in await store.load("run-2")] == ["乙的"]


@pytest.mark.asyncio
async def test_unknown_run_loads_empty(store):
    assert await store.load("run-does-not-exist") == []


@pytest.mark.asyncio
async def test_runs_lists_recorded_runs_newest_first(store):
    await store.append("run-1", [_entry(0)])
    await store.append("run-2", [_entry(0)])

    runs = await store.runs()

    assert [r.run_id for r in runs] == ["run-2", "run-1"]
    assert all(r.updated_at for r in runs)


@pytest.mark.asyncio
async def test_empty_store_has_no_runs(store):
    assert await store.runs() == []


# --------------------------------------------------------------------------- #
# JSONL 实现专有
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_jsonl_writes_one_line_per_entry(tmp_path):
    store = JsonlJournalStore(tmp_path / "journal")

    await store.append("run-1", [_entry(0, "甲"), _entry(1, "乙")])

    path = tmp_path / "journal" / "run-1.jsonl"
    assert path.is_file()
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["seq"] == 0


@pytest.mark.asyncio
async def test_jsonl_creates_parent_directories(tmp_path):
    store = JsonlJournalStore(tmp_path / "深" / "几" / "层")

    await store.append("run-1", [_entry(0)])

    assert await store.load("run-1") != []


@pytest.mark.asyncio
async def test_jsonl_skips_blank_and_truncated_lines(tmp_path):
    """硬杀进程会留下半截行。读的时候跳过它，而不是让整个回放炸掉。"""
    store = JsonlJournalStore(tmp_path / "journal")
    await store.append("run-1", [_entry(0), _entry(1)])

    path = tmp_path / "journal" / "run-1.jsonl"
    path.write_text(
        path.read_text(encoding="utf-8") + "\n" + '{"seq": 2, "event": {"type": "mess',  # 截断行
        encoding="utf-8",
    )

    entries = await store.load("run-1")

    assert [e.seq for e in entries] == [0, 1]


@pytest.mark.asyncio
async def test_jsonl_rejects_run_ids_that_would_escape_the_directory(tmp_path):
    """run_id 来自 URL 而会成为文件名——路径穿越必须在这里被挡住。"""
    store = JsonlJournalStore(tmp_path / "journal")

    for evil in ["../../etc/passwd", "a/b", "..", "", "x" * 65]:
        with pytest.raises(ValueError):
            await store.load(evil)


@pytest.mark.asyncio
async def test_jsonl_accepts_str_path(tmp_path):
    store = JsonlJournalStore(str(tmp_path / "journal"))

    await store.append("run-1", [_entry(0)])

    assert [e.seq for e in await store.load("run-1")] == [0]


@pytest.mark.asyncio
async def test_jsonl_runs_only_lists_jsonl_files(tmp_path):
    store = JsonlJournalStore(tmp_path / "journal")
    await store.append("run-1", [_entry(0)])
    (tmp_path / "journal" / "README.txt").write_text("别把我当运行记录", encoding="utf-8")

    assert [r.run_id for r in await store.runs()] == ["run-1"]
