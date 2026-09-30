"""RunJournal：一次运行的 RunEvent 序列及其可插拔存储。

事件源是编排器的输出流（ADR-0006），journal 只是这条流上的一个消费者——与 SSE
并列，而不是另起一套埋点。记录按**会话（thread）**组织，落盘前做归并（compaction）。
"""

from evaharness.journal.compaction import MessageCompactor, compact, is_compactable_text, merge_key
from evaharness.journal.journal import DEFAULT_FLUSH_THRESHOLD, RunJournal, journaled
from evaharness.journal.stores import (
    JournalEntry,
    JournalStore,
    JournalThread,
    JsonlJournalStore,
    MemoryJournalStore,
    summarize_as_preview,
    summarize_as_title,
    validate_id,
)

__all__ = [
    "DEFAULT_FLUSH_THRESHOLD",
    "JournalEntry",
    "JournalStore",
    "JournalThread",
    "JsonlJournalStore",
    "MemoryJournalStore",
    "MessageCompactor",
    "RunJournal",
    "compact",
    "is_compactable_text",
    "journaled",
    "merge_key",
    "summarize_as_preview",
    "summarize_as_title",
    "validate_id",
]
