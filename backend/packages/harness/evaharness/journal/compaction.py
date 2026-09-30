"""写盘归并：把同一条助手消息的逐 token 增量压成一条 journal 记录。

**为什么需要**：`messages` 事件是逐 token 的，每条都要带一份完整的框架 metadata。
实测一次运行 70204 字节里，`messages` 占 68077（96%），而其中**正文只有 266 字节、
metadata 有 39264 字节——148 倍**。真正的浪费不是正文，是同一份 metadata 被重复了
几百遍。

**归并的是什么**：同一个任务、同一份 metadata 的**全部纯文本增量**。它们本来就属于
同一条助手消息，逐 token 拆开只是流式的副产物。实测 240 条 → 约 3 条（~1%）。

**为什么可以跨交错归并**（而不只是合并相邻的）：前端 reducer 对文本只做「按
``task_id`` 追加」，气泡顺序由 ``task_start`` 决定（与文本到达序无关），工具行来自
**不参与归并**的 tool chunk。所以并行任务交错到达的 token 归并后，喂 reducer 得到的
状态与逐条喂**完全相同**。这条有测试兜底（test_journal_compaction.py），不是推理。

**代价（写进 ADR）**：逐 token 边界与并行交错顺序不再保留——事后还原不出「token 是
怎么一个个来的」。可观测状态不受影响。

**纯函数**：不改动入参。tee 必须能把**原事件**原样传给下游 SSE，归并只影响落盘。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from evaharness.agents.plan_exec.events import MessagesEvent, RunEvent
from evaharness.journal.stores import JournalEntry

MergeKey = tuple[str, str]


def is_compactable_text(event: RunEvent) -> bool:
    """这条事件是不是「纯文本增量」——可归并的最小单位。

    判据刻意严格：只要沾了工具（工具调用参数、工具结果），就不归并。工具行是前端
    时间线与气泡徽章的来源，必须与实时逐条对应。
    """
    if not isinstance(event, MessagesEvent):
        return False
    chunk = event.chunk
    if chunk.get("type") == "tool":
        return False
    content = chunk.get("content")
    if not isinstance(content, str) or not content:
        return False
    return not (chunk.get("tool_calls") or chunk.get("tool_call_chunks"))


def merge_key(event: RunEvent) -> MergeKey | None:
    """归并分组键：``(task_id, metadata 签名)``；不可归并时返回 ``None``。

    metadata 进键是为了**不跨 LLM 调用/图节点归并**：同一个任务的不同轮次
    （``langgraph_step`` 不同）各自成组，于是每组的 metadata 依然完整保留一份。
    """
    if not is_compactable_text(event):
        return None
    assert isinstance(event, MessagesEvent)  # is_compactable_text 已保证
    return (event.task_id, json.dumps(event.metadata, sort_keys=True, ensure_ascii=False))


def _merged(previous: JournalEntry, incoming: JournalEntry) -> JournalEntry:
    """把 *incoming* 的正文并进 *previous*（保留首条的其余字段与 metadata）。"""
    assert isinstance(previous.event, MessagesEvent) and isinstance(incoming.event, MessagesEvent)
    chunk = {**previous.event.chunk, "content": previous.event.chunk["content"] + incoming.event.chunk["content"]}
    return previous.model_copy(update={"event": previous.event.model_copy(update={"chunk": chunk})})


class MessageCompactor:
    """累加器：逐条喂入、就地归并。``RunJournal`` 用它保持每条事件 O(1)。

    ``seq`` 沿用**该组首条**的编号，被归并掉的编号就此空缺——所以一个 run 的 seq 是
    **严格递增但有跳号**的：它表达顺序，不是计数。
    """

    def __init__(self) -> None:
        self._entries: list[JournalEntry] = []
        self._index: dict[MergeKey, int] = {}

    def add(self, entry: JournalEntry) -> None:
        key = merge_key(entry.event)
        if key is None:
            self._entries.append(entry)
            return
        position = self._index.get(key)
        if position is None:
            self._index[key] = len(self._entries)
            self._entries.append(entry)
            return
        self._entries[position] = _merged(self._entries[position], entry)

    @property
    def entries(self) -> list[JournalEntry]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)


def compact(entries: Sequence[JournalEntry]) -> list[JournalEntry]:
    """批量版本，等价于逐条 :meth:`MessageCompactor.add` 后取 ``entries``。"""
    compactor = MessageCompactor()
    for entry in entries:
        compactor.add(entry)
    return compactor.entries


__all__ = ["MergeKey", "MessageCompactor", "compact", "is_compactable_text", "merge_key"]
