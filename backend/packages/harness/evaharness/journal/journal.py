"""RunJournal：在编排器的事件流上再挂一个消费者。

**它不是 LangChain 的 callback handler**（参照项目 DeerFlow 是那样做的）。区别的
根源在于：DeerFlow 没有统一的事件流，只能靠 ``config["callbacks"]`` 注册回调、从
``on_chain_start`` / ``on_llm_end`` 里逐个捕获事件；而 EvaAI 的编排器本来就产出一条
RunEvent 流（ADR-0006：「事件流本身就是 SSE 与运行时间线的共同来源」）。所以
journal 在这里就是一个 **tee**——同一份事件喂 SSE，也喂 journal。

顺带的好处：写入天然串行，不需要 per-thread 写锁。

**落盘前会做写盘归并**（见 compaction.py）：同一条助手消息的逐 token 增量压成一条，
体积从 240 条约到 3 条。这**只影响落盘**——tee 仍然把原事件原样透传给 SSE。

**记账失败不拖垮运行**：journal 是可观测设施，不是关键路径。:func:`journaled` 会把
store 的异常吞成警告——磁盘满了不该让用户的请求失败。但 :meth:`RunJournal.flush`
自身会抛（缓冲原样留着），好让直接调用它的代码能察觉。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from evaharness.agents.plan_exec.events import RunEvent, TaskEndEvent
from evaharness.journal.compaction import MessageCompactor
from evaharness.journal.stores import JournalEntry, JournalStore

logger = logging.getLogger(__name__)

DEFAULT_FLUSH_THRESHOLD = 20
"""攒够多少条**归并后**的记录才落一次 store。

注意它数的是归并后的条目，不是事件数：归并让一次运行通常只产出个位数条目，所以
这个阈值很少被触发，「什么时候落盘」实际上由 :class:`TaskEndEvent` 与收尾的
``flush()`` 决定（见 :meth:`RunJournal.record`）。
"""


class RunJournal:
    """把一段会话（thread）的事件流记进 store（落盘前归并）。

    不需要 ``run_id``：一个会话一个文件，而每次运行的身份由它自己的 ``run_start``
    事件承载——写进去的是事件本身，不是「哪一次运行」这个坐标。
    """

    def __init__(
        self,
        store: JournalStore,
        thread_id: str,
        *,
        flush_threshold: int = DEFAULT_FLUSH_THRESHOLD,
    ) -> None:
        self.store = store
        self.thread_id = thread_id
        self.flush_threshold = max(1, flush_threshold)
        self._compactor = MessageCompactor()
        self._seq = 0

    async def record(self, event: RunEvent) -> None:
        """记一条事件（先归并）；够阈值或任务收尾时落 store。"""
        self._seq += 1
        self._compactor.add(JournalEntry(seq=self._seq, event=event))
        # 任务结束是天然的提交点：归并让阈值很少触发，只靠阈值的话崩溃窗口会很长。
        if len(self._compactor) >= self.flush_threshold or isinstance(event, TaskEndEvent):
            await self.flush()

    async def flush(self) -> None:
        """把缓冲里的记录落进 store，并**关闭**已落盘的归并组。

        归并组一旦落盘就不再接纳新正文——后续到达的同组正文会另起一条。这样做是
        因为 JSONL 只能追加、不能改写：把新正文并进已落盘的那条会让磁盘上的副本
        过期。代价是跨 flush 边界的同组会拆成两条，但**正文顺序不变**（后写的在
        后面），拼接结果与归并成一条时相同。
        """
        entries = self._compactor.entries
        if not entries:
            return
        await self.store.append(self.thread_id, entries)
        # 落盘成功才重置；失败时缓冲原样留着，下次再试，不丢数据。
        self._compactor = MessageCompactor()

    async def replay(self) -> list[JournalEntry]:
        """取出这个会话的全部记录（先把缓冲落下去，否则会漏掉尾巴）。"""
        await self.flush()
        return await self.store.load(self.thread_id)


async def journaled(events: AsyncIterator[RunEvent], journal: RunJournal) -> AsyncIterator[RunEvent]:
    """在事件流上做记账，原样透传给下游。

    tee 必须透明：下游（SSE）拿到的是**同一个对象、同一个顺序**——归并只发生在
    journal 自己的缓冲里，不回写事件。
    """
    try:
        async for event in events:
            try:
                await journal.record(event)
            except Exception:
                logger.warning("RunJournal 记账失败，跳过该事件", exc_info=True)
            yield event
    finally:
        # 取消（BaseException）与异常都会走到这里，让断开前发生的事留下痕迹。
        try:
            await journal.flush()
        except Exception:
            logger.warning("RunJournal 收尾落盘失败", exc_info=True)


__all__ = ["DEFAULT_FLUSH_THRESHOLD", "RunJournal", "journaled"]
