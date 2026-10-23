"""去抖队列：合并、重置、丢弃、异常隔离、epoch。

**不用 sleep 造时序**：本机 Windows 上亚 16ms 的 ``asyncio.sleep`` 会提前触发。这里要么直接
``flush()``，要么用 ``debounce_seconds=0`` + ``Event`` 事件驱动地等。
"""

from __future__ import annotations

import asyncio

import pytest

from evaharness.memory.capture import CapturedTurn, CaptureResult
from evaharness.memory.queue import MemoryQueue


def _capture(message_id: str = "a1") -> CaptureResult:
    return CaptureResult(
        turns=(CapturedTurn(user_text="用户说了什么", assistant_text="助手回答", last_message_id=message_id),),
        last_complete_ai_id=message_id,
    )


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], str | None]] = []
        self.stale: list[bool] = []
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def __call__(self, thread_id, turns, last_message_id, *, is_stale=lambda: False):
        self.started.set()
        await self.release.wait()
        self.calls.append((thread_id, [turn.user_text for turn in turns], last_message_id))
        self.stale.append(is_stale())


@pytest.fixture
def recorder():
    recorder = Recorder()
    recorder.release.set()
    return recorder


@pytest.mark.asyncio
async def test_flush_processes_the_pending_capture(recorder):
    queue = MemoryQueue(recorder, debounce_seconds=3600)
    try:
        queue.submit("th-1", _capture())
        await queue.flush()
    finally:
        await queue.aclose()

    assert recorder.calls == [("th-1", ["用户说了什么"], "a1")]
    assert queue.backlog == 0


@pytest.mark.asyncio
async def test_a_new_submit_resets_the_timer(recorder):
    queue = MemoryQueue(recorder, debounce_seconds=3600)
    try:
        queue.submit("th-1", _capture("a1"))
        first = queue._timers["th-1"]
        queue.submit("th-1", _capture("a2"))
        second = queue._timers["th-1"]
    finally:
        await queue.aclose()

    assert first.cancelled()
    assert second.when() >= first.when()


@pytest.mark.asyncio
async def test_submitting_an_incomplete_capture_is_ignored(recorder):
    queue = MemoryQueue(recorder, debounce_seconds=3600)
    try:
        queue.submit("th-1", CaptureResult(turns=(), last_complete_ai_id=None))
        assert queue.backlog == 0
    finally:
        await queue.aclose()


@pytest.mark.asyncio
async def test_aclose_discards_pending_work(recorder):
    queue = MemoryQueue(recorder, debounce_seconds=3600)
    queue.submit("th-1", _capture())

    await queue.aclose()

    assert recorder.calls == []


@pytest.mark.asyncio
async def test_a_closed_queue_ignores_new_submissions(recorder):
    queue = MemoryQueue(recorder, debounce_seconds=3600)
    await queue.aclose()

    queue.submit("th-1", _capture())

    assert queue.backlog == 0


@pytest.mark.asyncio
async def test_one_failing_thread_does_not_kill_the_worker():
    done = asyncio.Event()
    calls: list[str] = []

    async def update(thread_id, turns, last_message_id, *, is_stale=lambda: False):
        if thread_id == "bad":
            raise RuntimeError("抽取挂了")
        calls.append(thread_id)
        done.set()

    queue = MemoryQueue(update, debounce_seconds=0)
    try:
        queue.submit("bad", _capture())
        queue.submit("good", _capture())
        await asyncio.wait_for(done.wait(), timeout=2)
    finally:
        await queue.aclose()

    assert calls == ["good"]


@pytest.mark.asyncio
async def test_clear_discards_pending_work(recorder):
    queue = MemoryQueue(recorder, debounce_seconds=3600)
    try:
        queue.submit("th-1", _capture())
        queue.clear()
        await queue.flush()
    finally:
        await queue.aclose()

    assert recorder.calls == []
    assert queue.epoch == 1


@pytest.mark.asyncio
async def test_an_in_flight_update_is_marked_stale_after_clear(recorder):
    recorder.release.clear()
    queue = MemoryQueue(recorder, debounce_seconds=3600)
    try:
        queue.submit("th-1", _capture())
        task = asyncio.create_task(queue.flush())
        await recorder.started.wait()
        queue.clear()
        recorder.release.set()
        await task
    finally:
        await queue.aclose()

    assert recorder.stale == [True]
