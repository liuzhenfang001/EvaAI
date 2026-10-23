"""抽取的去抖队列：每段会话安静 ``debounce_seconds`` 秒后，把这一轮攒下的对话交给 updater。

**为什么需要队列**：抽取要调一次 LLM。每一轮对话结束就抽一次，既费钱又把「用户还在连续
输入」的中间态也抽了进去。去抖把「最后一轮之后安静 30 秒」当作「这段对话告一段落」。

**每 thread 只留最新一份 capture**：capture 本身就是「watermark 之后的完整窗口」，后一份是
前一份的超集（除非中间成功抽取把 watermark 推进了——那时前一份的内容已经落库，覆盖掉也不亏）。
这样内存占用是 ``thread 数``，不是「积压消息数」。

**单 worker 串行**：同一时刻只有一个抽取在跑（与参照项目同一取舍）。一条慢抽取会阻塞别的
thread，日志里能看到 backlog——这是有意换来的简单性：抽取是后台活，晚几秒没有关系。

**epoch 是给「清空全部」用的**：REST 的 ``DELETE /memory`` 会在抽取飞行途中把库清空。递增
epoch 后，正在飞的那次结果在落库前会被判定为过期，不会把用户刚抹掉的东西又写回来。

**测试不要靠 sleep**：本机 Windows 上亚 16ms 的 ``asyncio.sleep`` 会提前触发（实测）。
队列因此暴露 :meth:`MemoryQueue.flush`，测试直接调它；要测定时器本身就用
``debounce_seconds=0`` + 事件等待，而不是猜一个 sleep 时长。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

from evaharness.memory.capture import CapturedTurn, CaptureResult

logger = logging.getLogger(__name__)


class TurnUpdater(Protocol):
    """队列对 updater 的唯一要求：处理一段会话的若干轮。"""

    async def __call__(
        self,
        thread_id: str,
        turns: Sequence[CapturedTurn],
        last_message_id: str | None,
        *,
        is_stale: Callable[[], bool] = lambda: False,
    ) -> None: ...


@dataclass(frozen=True)
class _Pending:
    capture: CaptureResult
    epoch: int


class MemoryQueue:
    """按会话去抖的抽取队列。"""

    def __init__(
        self,
        update: TurnUpdater,
        *,
        debounce_seconds: float = 30.0,
        name: str = "evaai-memory-queue",
    ) -> None:
        """**必须在事件循环里构造**（lifespan / async 测试）——定时器要绑 loop。"""
        self._update = update
        self._debounce = max(0.0, float(debounce_seconds))
        self._name = name
        self._loop = asyncio.get_running_loop()
        self._pending: dict[str, _Pending] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._ready: asyncio.Queue[str] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._epoch = 0
        self._closed = False

    @property
    def backlog(self) -> int:
        """还没处理的会话数（日志与测试用）。"""
        return len(self._pending)

    @property
    def epoch(self) -> int:
        return self._epoch

    def submit(self, thread_id: str, capture: CaptureResult) -> None:
        """登记一次 capture 并重置这个会话的定时器。

        capture 里没有任何「完整轮次」（``last_complete_ai_id is None``）就什么都不做——
        被取消、没有助手回复的轮次不该触发抽取，也不该让水位线前进。
        """
        if self._closed:
            return
        if capture.last_complete_ai_id is None:
            return
        self._pending[thread_id] = _Pending(capture=capture, epoch=self._epoch)
        timer = self._timers.pop(thread_id, None)
        if timer is not None:
            timer.cancel()
        self._timers[thread_id] = self._loop.call_later(self._debounce, self._fire, thread_id)
        self._ensure_worker()

    async def flush(self, thread_id: str | None = None) -> None:
        """立刻处理某个会话（``None`` = 全部待处理项）。测试缝，也用于清空前的收尾。"""
        async with self._lock:
            thread_ids = [thread_id] if thread_id is not None else list(self._pending)
            for tid in thread_ids:
                pending = self._pending.pop(tid, None)
                timer = self._timers.pop(tid, None)
                if timer is not None:
                    timer.cancel()
                if pending is None or pending.epoch != self._epoch or self._closed:
                    continue
                try:
                    await self._update(
                        tid,
                        pending.capture.turns,
                        pending.capture.last_complete_ai_id,
                        is_stale=lambda epoch=pending.epoch: self._closed or self._epoch != epoch,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # 一次抽取失败不该杀死 worker，也不该影响别的会话。
                    logger.warning("记忆抽取失败：thread=%s", tid, exc_info=True)

    def clear(self) -> None:
        """丢弃所有待处理项，并让正在飞的抽取作废（REST「清空全部」用）。"""
        self._epoch += 1
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        self._pending.clear()

    async def aclose(self) -> None:
        """关机：取消定时器与 worker，**丢弃未决项**（不 flush、不推进水位线）。

        丢弃的是「这次没做」，不是「永远不做」——下次用户回到这个会话说话，capture 会从
        旧水位线把这些轮次一起带上。
        """
        self._closed = True
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        self._pending.clear()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    # ---- 内部 ----------------------------------------------------------------

    def _fire(self, thread_id: str) -> None:
        self._timers.pop(thread_id, None)
        if self._closed or thread_id not in self._pending:
            return
        self._ready.put_nowait(thread_id)

    def _ensure_worker(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        self._worker = self._loop.create_task(self._run(), name=self._name)

    async def _run(self) -> None:
        while True:
            thread_id = await self._ready.get()
            if self._closed:
                return
            if self._pending:
                logger.debug("记忆队列开始处理：thread=%s backlog=%d", thread_id, len(self._pending))
            try:
                await self.flush(thread_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("记忆队列处理失败：thread=%s", thread_id, exc_info=True)


__all__ = ["MemoryQueue", "TurnUpdater"]
