"""RunJournal：一次运行的 RunEvent 序列及其可插拔存储。

事件源是编排器的输出流（ADR-0006），journal 只是这条流上的一个消费者——与 SSE
并列，而不是另起一套埋点。
"""

from evaharness.journal.journal import DEFAULT_FLUSH_THRESHOLD, RunJournal, journaled
from evaharness.journal.stores import (
    JournalEntry,
    JournalRun,
    JournalStore,
    JsonlJournalStore,
    MemoryJournalStore,
)

__all__ = [
    "DEFAULT_FLUSH_THRESHOLD",
    "JournalEntry",
    "JournalRun",
    "JournalStore",
    "JsonlJournalStore",
    "MemoryJournalStore",
    "RunJournal",
    "journaled",
]
