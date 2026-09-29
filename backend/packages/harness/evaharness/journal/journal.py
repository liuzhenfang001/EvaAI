"""RunJournal：在编排器的事件流上再挂一个消费者。

**它不是 LangChain 的 callback handler**（参照项目 DeerFlow 是那样做的）。区别的
根源在于：DeerFlow 没有统一的事件流，只能靠 ``config["callbacks"]`` 注册回调、从
``on_chain_start`` / ``on_llm_end`` 里逐个捕获事件；而 EvaAI 的编排器本来就产出一条
RunEvent 流（ADR-0006：「事件流本身就是 SSE 与 T3 运行时间线的共同来源」）。所以
journal 在这里就是一个 **tee**——同一份事件喂 SSE，也喂 journal。

顺带的好处：写入天然串行，不需要 per-thread 写锁。

**记账失败不拖垮运行**：journal 是可观测设施，不是关键路径。:func:`journaled` 会把
store 的异常吞成警告——磁盘满了不该让用户的请求失败。但 :meth:`RunJournal.flush`
自身会抛（并已把缓冲放回），好让直接调用它的代码能察觉。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from evaharness.agents.plan_exec.events import RunEvent
from evaharness.journal.stores import JournalEntry, JournalStore

logger = logging.getLogger(__name__)

DEFAULT_FLUSH_THRESHOLD = 20
"""攒够多少条记录才落一次 store。

不能是 1：``MessagesEvent`` 是**逐 token** 的，一次运行会产生成百上千条；每条都
触发一次文件写（JSONL store 是「一次批量 = 一次开/写/关文件」）会把实时流拖慢。
"""


class RunJournal:
    """记录一次运行的完整 RunEvent 序列。"""

    def __init__(self, store: JournalStore, run_id: str, *, flush_threshold: int = DEFAULT_FLUSH_THRESHOLD) -> None:
        self.store = store
        self.run_id = run_id
        self.flush_threshold = max(1, flush_threshold)
        self._buffer: list[JournalEntry] = []
        self._seq = 0

    async def record(self, event: RunEvent) -> None:
        """记一条事件；缓冲满则落 store。"""
        self._seq += 1
        self._buffer.append(JournalEntry(seq=self._seq, event=event))
        if len(self._buffer) >= self.flush_threshold:
            await self.flush()

    async def flush(self) -> None:
        """把缓冲里的记录落进 store。

        落盘失败时把记录**放回缓冲**（下次再试），然后把异常抛出去——这样调用方
        既不会丢数据，也能察觉出问题了。
        """
        if not self._buffer:
            return
        batch = self._buffer
        self._buffer = []
        try:
            await self.store.append(self.run_id, batch)
        except Exception:
            self._buffer = batch + self._buffer
            raise

    async def replay(self) -> list[JournalEntry]:
        """取出本次运行的全部记录（先把缓冲落下去，否则会漏掉尾巴）。"""
        await self.flush()
        return await self.store.load(self.run_id)


async def journaled(events: AsyncIterator[RunEvent], journal: RunJournal) -> AsyncIterator[RunEvent]:
    """在事件流上做记账，原样透传给下游。

    tee 必须透明：下游（SSE）拿到的是**同一个对象、同一个顺序**。
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
