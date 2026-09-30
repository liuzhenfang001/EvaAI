"""契约测试：RunJournal 与 journaled（事件流上的记账 tee）。

RunJournal 是**事件流的 sink**，不是 LangChain 的 callback handler——EvaAI 的编排器
本来就产出一条 RunEvent 流（ADR-0006），journal 只是在这条流上再挂一个消费者。
所以它的写入天然是串行的，不需要参照项目那套 per-thread 写锁（ADR-0008）。
"""

import asyncio
from collections.abc import AsyncIterator

import pytest

from evaharness.agents.plan_exec.events import MessagesEvent, TaskEndEvent, TaskStartEvent
from evaharness.journal.journal import RunJournal, journaled
from evaharness.journal.stores import MemoryJournalStore

THREAD = "th-1"


def _text(text: str, *, task_id: str = "t1", step: int = 7) -> MessagesEvent:
    return MessagesEvent(
        task_id=task_id,
        chunk={"content": text, "type": "AIMessageChunk"},
        metadata={"langgraph_step": step},
    )


def _other_task(text: str) -> TaskStartEvent:
    return TaskStartEvent(task_id=text, description=f"任务 {text}")


async def _source(events, *, raises: BaseException | None = None) -> AsyncIterator:
    for event in events:
        yield event
    if raises is not None:
        raise raises


def _journal(store, *, flush_threshold: int = 100) -> RunJournal:
    return RunJournal(store, THREAD, flush_threshold=flush_threshold)


# --------------------------------------------------------------------------- #
# seq 与归并
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_seq_is_strictly_increasing():
    store = MemoryJournalStore()
    journal = _journal(store)

    for name in ["t1", "t2", "t3"]:
        await journal.record(_other_task(name))
    await journal.flush()

    seqs = [entry.seq for entry in await store.load(THREAD)]
    assert seqs == sorted(set(seqs)), "seq 必须严格递增（归并会吃掉编号，所以有跳号）"


@pytest.mark.asyncio
async def test_text_chunks_are_compacted_before_persisting():
    """落盘的是归并后的结果——240 条变个位数，这是 journal 不失控的关键。"""
    store = MemoryJournalStore()
    journal = _journal(store)

    for char in "现在几点？":
        await journal.record(_text(char))
    await journal.flush()

    entries = await store.load(THREAD)
    assert len(entries) == 1
    assert entries[0].event.chunk["content"] == "现在几点？"


@pytest.mark.asyncio
async def test_compaction_keeps_full_metadata():
    """删字段不是体积方案——metadata 完整保留一份（用户决策，见 ADR-0009）。"""
    store = MemoryJournalStore()
    journal = _journal(store)
    full = {"langgraph_step": 7, "langgraph_node": "model", "lc_versions": {"core": "1.6.5"}}
    event = MessagesEvent(task_id="t1", chunk={"content": "甲", "type": "AIMessageChunk"}, metadata=full)

    await journal.record(event)
    await journal.flush()

    assert (await store.load(THREAD))[0].event.metadata == full


# --------------------------------------------------------------------------- #
# 什么时候落盘
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_nothing_reaches_the_store_before_a_commit_point():
    store = MemoryJournalStore()
    journal = _journal(store)

    await journal.record(_other_task("t1"))
    await journal.record(_other_task("t2"))

    assert await store.load(THREAD) == []


@pytest.mark.asyncio
async def test_task_end_forces_a_flush():
    """任务结束是天然的提交点。

    归并让一次运行只产出个位数条目，按条目数计的阈值因此很少触发——只靠阈值的话，
    崩在中途会丢掉整次运行。这条把它压回单个任务。
    """
    store = MemoryJournalStore()
    journal = _journal(store)

    await journal.record(_text("甲"))
    await journal.record(TaskEndEvent(task_id="t1", status="done", output="甲"))

    assert [entry.event.type for entry in await store.load(THREAD)] == ["messages", "task_end"]


@pytest.mark.asyncio
async def test_threshold_triggers_a_flush():
    store = MemoryJournalStore()
    journal = _journal(store, flush_threshold=3)

    for name in ["t1", "t2", "t3"]:
        await journal.record(_other_task(name))

    assert len(await store.load(THREAD)) == 3


@pytest.mark.asyncio
async def test_flush_closes_the_group_so_later_text_starts_a_new_entry():
    """JSONL 只能追加不能改写，所以已落盘的组不再接纳新正文。

    代价是同组跨 flush 会拆成两条；收益是**磁盘上的副本永不过期**。正文顺序不变，
    拼接结果与归并成一条时相同。
    """
    store = MemoryJournalStore()
    journal = _journal(store)

    await journal.record(_text("甲"))
    await journal.flush()
    await journal.record(_text("乙"))
    await journal.flush()

    entries = await store.load(THREAD)
    assert [entry.event.chunk["content"] for entry in entries] == ["甲", "乙"]
    assert entries[0].seq < entries[1].seq, "后写的必须排在后面，拼接顺序才不会乱"


@pytest.mark.asyncio
async def test_flush_is_idempotent():
    store = MemoryJournalStore()
    journal = _journal(store)

    await journal.record(_text("甲"))
    await journal.flush()
    await journal.flush()

    assert len(await store.load(THREAD)) == 1


@pytest.mark.asyncio
async def test_a_failing_store_keeps_the_buffer_for_retry():
    class FlakyStore(MemoryJournalStore):
        def __init__(self) -> None:
            super().__init__()
            self.fail = True

        async def append(self, thread_id, entries):
            if self.fail:
                raise RuntimeError("磁盘满了")
            await super().append(thread_id, entries)

    store = FlakyStore()
    journal = _journal(store)
    await journal.record(_text("甲"))
    with pytest.raises(RuntimeError):
        await journal.flush()

    store.fail = False
    await journal.flush()

    assert [entry.event.chunk["content"] for entry in await store.load(THREAD)] == ["甲"]


@pytest.mark.asyncio
async def test_replay_flushes_first_so_buffered_events_are_visible():
    store = MemoryJournalStore()
    journal = _journal(store)
    await journal.record(_text("还没落盘"))

    entries = await journal.replay()

    assert [entry.event.chunk["content"] for entry in entries] == ["还没落盘"]


# --------------------------------------------------------------------------- #
# journaled：tee
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_journaled_passes_events_through_untouched():
    """tee 必须透明：原对象、原顺序、**原内容**。

    归并只发生在 journal 自己的缓冲里；如果它回写到事件上，SSE 就会收到被拼过的帧，
    前端的流式吐字会变成一次性蹦出一大段。
    """
    store = MemoryJournalStore()
    journal = _journal(store)
    events = [_text("现"), _text("在"), _text("几"), _text("点"), TaskEndEvent(task_id="t1", status="done")]

    seen = [event async for event in journaled(_source(events), journal)]

    assert [event is original for event, original in zip(seen, events, strict=True)] == [True] * 5
    assert [event.chunk["content"] for event in seen if isinstance(event, MessagesEvent)] == [
        "现",
        "在",
        "几",
        "点",
    ]


@pytest.mark.asyncio
async def test_journaled_flushes_at_the_end():
    store = MemoryJournalStore()
    journal = _journal(store)

    async for _ in journaled(_source([_text("甲"), _text("乙")]), journal):
        pass

    assert [entry.event.chunk["content"] for entry in await store.load(THREAD)] == ["甲乙"]


@pytest.mark.asyncio
async def test_journaled_flushes_even_when_the_source_raises():
    """流到一半炸了，前面发生的事更要留下——那正是排查的依据。"""
    store = MemoryJournalStore()
    journal = _journal(store)

    with pytest.raises(RuntimeError, match="炸了"):
        async for _ in journaled(_source([_text("甲")], raises=RuntimeError("炸了")), journal):
            pass

    assert [entry.event.chunk["content"] for entry in await store.load(THREAD)] == ["甲"]


@pytest.mark.asyncio
async def test_journaled_does_not_swallow_cancellation():
    store = MemoryJournalStore()
    journal = _journal(store)

    async def source():
        yield _text("甲")
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
    assert [entry.event.chunk["content"] for entry in await store.load(THREAD)] == ["甲"]


@pytest.mark.asyncio
async def test_journaled_survives_a_failing_store():
    """记账失败不该把整次运行带下水——journal 是可观测设施，不是关键路径。"""

    class ExplodingStore(MemoryJournalStore):
        async def append(self, thread_id, entries):
            raise RuntimeError("磁盘满了")

    journal = _journal(ExplodingStore(), flush_threshold=1)

    seen = [event async for event in journaled(_source([_text("甲"), _text("乙")]), journal)]

    assert [event.chunk["content"] for event in seen] == ["甲", "乙"]
