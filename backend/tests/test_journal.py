"""契约测试：RunJournal 与 journaled（事件流上的记账 tee）。

RunJournal 是**事件流的 sink**，不是 LangChain 的 callback handler——EvaAI 的编排器
本来就产出一条 RunEvent 流（ADR-0006），journal 只是在这条流上再挂一个消费者。
所以它的写入天然是串行的，不需要参照项目那套 per-thread 写锁（见 ADR-0008）。
"""

import asyncio
from collections.abc import AsyncIterator

import pytest

from evaharness.agents.plan_exec.events import MessagesEvent, TaskEndEvent
from evaharness.journal.journal import RunJournal, journaled
from evaharness.journal.stores import MemoryJournalStore


def _msg(text: str) -> MessagesEvent:
    return MessagesEvent(task_id="t1", chunk={"content": text})


async def _source(texts: list[str], *, raises: BaseException | None = None) -> AsyncIterator:
    for text in texts:
        yield _msg(text)
    if raises is not None:
        raise raises


# --------------------------------------------------------------------------- #
# seq 与缓冲
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_seq_starts_at_one_and_increments():
    store = MemoryJournalStore()
    journal = RunJournal(store, "run-1", flush_threshold=100)

    for text in ["甲", "乙", "丙"]:
        await journal.record(_msg(text))
    await journal.flush()

    assert [entry.seq for entry in await store.load("run-1")] == [1, 2, 3]


@pytest.mark.asyncio
async def test_nothing_reaches_the_store_before_the_threshold():
    """逐 token 落盘会让每个 token 都触发一次文件写，所以必须缓冲。"""
    store = MemoryJournalStore()
    journal = RunJournal(store, "run-1", flush_threshold=3)

    await journal.record(_msg("甲"))
    await journal.record(_msg("乙"))

    assert await store.load("run-1") == []


@pytest.mark.asyncio
async def test_threshold_triggers_a_flush():
    store = MemoryJournalStore()
    journal = RunJournal(store, "run-1", flush_threshold=3)

    for text in ["甲", "乙", "丙"]:
        await journal.record(_msg(text))

    assert [entry.seq for entry in await store.load("run-1")] == [1, 2, 3]


@pytest.mark.asyncio
async def test_flush_writes_the_remainder_and_is_idempotent():
    store = MemoryJournalStore()
    journal = RunJournal(store, "run-1", flush_threshold=100)

    await journal.record(_msg("甲"))
    await journal.flush()
    await journal.flush()  # 再 flush 一次不得写出重复条目

    assert [entry.seq for entry in await store.load("run-1")] == [1]


@pytest.mark.asyncio
async def test_replay_flushes_first_so_buffered_events_are_visible():
    store = MemoryJournalStore()
    journal = RunJournal(store, "run-1", flush_threshold=100)
    await journal.record(_msg("还没落盘"))

    entries = await journal.replay()

    assert [entry.event.chunk["content"] for entry in entries] == ["还没落盘"]


@pytest.mark.asyncio
async def test_seq_survives_across_flushes():
    store = MemoryJournalStore()
    journal = RunJournal(store, "run-1", flush_threshold=2)

    for text in ["甲", "乙", "丙", "丁", "戊"]:
        await journal.record(_msg(text))

    assert [entry.seq for entry in await journal.replay()] == [1, 2, 3, 4, 5]


# --------------------------------------------------------------------------- #
# journaled：tee
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_journaled_passes_events_through_untouched():
    """tee 必须是透明的：原对象、原顺序，下游（SSE）察觉不到它的存在。"""
    store = MemoryJournalStore()
    journal = RunJournal(store, "run-1", flush_threshold=100)
    events = [_msg("甲"), _msg("乙"), TaskEndEvent(task_id="t1", status="done")]

    async def source():
        for event in events:
            yield event

    seen = [event async for event in journaled(source(), journal)]

    assert [event is original for event, original in zip(seen, events, strict=True)] == [True] * 3


@pytest.mark.asyncio
async def test_journaled_flushes_at_the_end():
    store = MemoryJournalStore()
    journal = RunJournal(store, "run-1", flush_threshold=100)

    async for _ in journaled(_source(["甲", "乙"]), journal):
        pass

    assert [entry.seq for entry in await store.load("run-1")] == [1, 2]


@pytest.mark.asyncio
async def test_journaled_flushes_even_when_the_source_raises():
    """流到一半炸了，前面发生的事更要留下——那正是排查的依据。"""
    store = MemoryJournalStore()
    journal = RunJournal(store, "run-1", flush_threshold=100)

    with pytest.raises(RuntimeError, match="炸了"):
        async for _ in journaled(_source(["甲"], raises=RuntimeError("炸了")), journal):
            pass

    assert [entry.event.chunk["content"] for entry in await store.load("run-1")] == ["甲"]


@pytest.mark.asyncio
async def test_journaled_does_not_swallow_cancellation():
    store = MemoryJournalStore()
    journal = RunJournal(store, "run-1", flush_threshold=100)

    async def source():
        yield _msg("甲")
        await asyncio.Event().wait()  # 永不满足，保证取消落在流中间

    async def consume():
        async for _ in journaled(source(), journal):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert [entry.event.chunk["content"] for entry in await store.load("run-1")] == ["甲"]


@pytest.mark.asyncio
async def test_journaled_survives_a_failing_store():
    """记账失败不该把整次运行带下水——journal 是可观测设施，不是关键路径。"""

    class ExplodingStore(MemoryJournalStore):
        async def append(self, run_id, entries):
            raise RuntimeError("磁盘满了")

    journal = RunJournal(ExplodingStore(), "run-1", flush_threshold=1)

    seen = [event async for event in journaled(_source(["甲", "乙"]), journal)]

    assert [event.chunk["content"] for event in seen] == ["甲", "乙"]
