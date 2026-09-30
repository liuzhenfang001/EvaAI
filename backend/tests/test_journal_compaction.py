"""契约测试：写盘归并（同一条助手消息的逐 token 增量 → 一条 journal 记录）。

这是本切片最需要被证明的地方，所以**等价性不用「reducer 的 Python 复刻」来表达**
（那只是自说自话）。改为两条非自我指涉的性质断言：

1. 每个任务的正文，按出现顺序拼接后**逐字相同**；
2. **不可归并的事件子序列**（工具调用、工具结果、各类生命周期事件）**逐条相同**。

前端 reducer 的可观测输出完全由这两者决定——正文按 task 追加、气泡顺序由
``task_start`` 决定、工具行来自 tool chunk。所以这两条成立 ⟺ 归并对渲染无损。
"""

import json

import pytest

from evaharness.agents.plan_exec.events import (
    MessagesEvent,
    PlanEvent,
    SummaryEvent,
    TaskEndEvent,
    TaskStartEvent,
)
from evaharness.agents.plan_exec.schemas import PlanTask
from evaharness.journal.compaction import (
    MessageCompactor,
    compact,
    is_compactable_text,
    merge_key,
)
from evaharness.journal.stores import JournalEntry


class _Seq:
    """给测试用的自增编号（真实场景里 seq 由 RunJournal 分配）。"""

    def __init__(self) -> None:
        self.n = 0

    def __next__(self) -> int:
        self.n += 1
        return self.n


def _entry(seq, event) -> JournalEntry:
    return JournalEntry(seq=seq, event=event)


def _text(seq, task_id: str, content: str, *, step: int = 7) -> JournalEntry:
    return _entry(
        seq,
        MessagesEvent(
            task_id=task_id,
            chunk={"content": content, "type": "AIMessageChunk"},
            metadata={"langgraph_step": step, "langgraph_node": "model", "thread_id": f"run:{task_id}"},
        ),
    )


def _tool_call(seq, task_id: str, name: str) -> JournalEntry:
    return _entry(
        seq,
        MessagesEvent(
            task_id=task_id,
            chunk={"content": "", "type": "AIMessageChunk", "tool_call_chunks": [{"name": name, "args": ""}]},
            metadata={"langgraph_step": 7, "thread_id": f"run:{task_id}"},
        ),
    )


def _tool_result(seq, task_id: str, name: str) -> JournalEntry:
    return _entry(
        seq,
        MessagesEvent(
            task_id=task_id,
            chunk={"content": "结果", "type": "tool", "name": name},
            metadata={"thread_id": f"run:{task_id}"},
        ),
    )


# --------------------------------------------------------------------------- #
# 等价性（本文件存在的理由）
# --------------------------------------------------------------------------- #


def _per_task_text(entries) -> dict[str, str]:
    """每个任务的正文，按出现顺序拼接——前端气泡的可观测内容。"""
    texts: dict[str, str] = {}
    for entry in entries:
        event = entry.event
        if isinstance(event, MessagesEvent) and is_compactable_text(event):
            texts[event.task_id] = texts.get(event.task_id, "") + event.chunk["content"]
    return texts


def _opaque_subsequence(entries):
    """不可归并的事件子序列——工具行与生命周期，必须逐条原样保留。"""
    return [entry.event for entry in entries if merge_key(entry.event) is None]


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda s: [_text(next(s), "t1", "甲"), _text(next(s), "t1", "乙"), _text(next(s), "t1", "丙")],
            id="单任务连续",
        ),
        pytest.param(
            lambda s: [
                _text(next(s), "t1", "甲"),
                _text(next(s), "t2", "一"),
                _text(next(s), "t1", "乙"),
                _text(next(s), "t2", "二"),
            ],
            id="并行交错",
        ),
        pytest.param(
            lambda s: [
                _text(next(s), "t1", "让我"),
                _tool_call(next(s), "t1", "get_current_time"),
                _text(next(s), "t1", "查一下"),
                _tool_result(next(s), "t1", "get_current_time"),
                _text(next(s), "t1", "现在是 18:04"),
            ],
            id="工具调用把正文切成多组",
        ),
        pytest.param(
            lambda s: [
                _entry(next(s), PlanEvent(tasks=[PlanTask(id="t1", description="d")])),
                _text(next(s), "t1", "甲"),
                _entry(next(s), TaskStartEvent(task_id="t2", description="d")),
                _text(next(s), "t2", "一"),
                _text(next(s), "t1", "乙"),
                _entry(next(s), TaskEndEvent(task_id="t1", status="done", output="甲")),
                _entry(next(s), SummaryEvent(status="completed", results=[])),
            ],
            id="混合生命周期事件",
        ),
    ],
)
def test_compaction_preserves_text_and_opaque_events(build):
    """归并后：正文逐字不变、不可归并的事件逐条不变。"""
    seq = _Seq()
    original = build(seq)

    compacted = compact(original)

    assert _per_task_text(compacted) == _per_task_text(original)
    assert _opaque_subsequence(compacted) == _opaque_subsequence(original)


# --------------------------------------------------------------------------- #
# 归并粒度
# --------------------------------------------------------------------------- #


def test_consecutive_text_chunks_collapse_into_one_entry():
    seq = _Seq()
    entries = [_text(next(seq), "t1", c) for c in "现在几点"]

    compacted = compact(entries)

    assert len(compacted) == 1
    assert compacted[0].event.chunk["content"] == "现在几点"


def test_parallel_interleaved_tasks_collapse_per_task():
    """交错到达也能压掉——这是体积从 240 条降到个位数的关键。"""
    seq = _Seq()
    entries = [
        _text(next(seq), "t1", "甲一"),
        _text(next(seq), "t2", "乙一"),
        _text(next(seq), "t1", "甲二"),
        _text(next(seq), "t2", "乙二"),
    ]

    compacted = compact(entries)

    assert [e.event.task_id for e in compacted] == ["t1", "t2"]
    assert [e.event.chunk["content"] for e in compacted] == ["甲一甲二", "乙一乙二"]


def test_different_metadata_stays_in_separate_groups():
    """同一个任务的不同 LLM 轮次（langgraph_step 不同）各自成组，互不吞并。"""
    seq = _Seq()
    entries = [
        _text(next(seq), "t1", "第一轮", step=3),
        _text(next(seq), "t1", "第二轮", step=5),
    ]

    compacted = compact(entries)

    assert [e.event.chunk["content"] for e in compacted] == ["第一轮", "第二轮"]
    assert [e.event.metadata["langgraph_step"] for e in compacted] == [3, 5]


def test_merged_entry_keeps_the_first_chunks_complete_metadata():
    """落盘保留完整 metadata：不重复，但一条不丢。

    这条是「不许在实现里悄悄退化成只留自有键」的守卫——见计划里的风险 4。
    """
    seq = _Seq()
    full = {"langgraph_step": 7, "langgraph_node": "model", "langchain_versions": {"core": "1.6.5"}}
    entries = [
        JournalEntry(
            seq=next(seq),
            event=MessagesEvent(task_id="t1", chunk={"content": "甲", "type": "AIMessageChunk"}, metadata=full),
        ),
        JournalEntry(
            seq=next(seq),
            event=MessagesEvent(task_id="t1", chunk={"content": "乙", "type": "AIMessageChunk"}, metadata=full),
        ),
    ]

    compacted = compact(entries)

    assert compacted[0].event.metadata == full


def test_merged_entry_keeps_the_groups_first_seq():
    """seq 表达顺序而非计数——被归并掉的编号空缺，这是有意的。"""
    seq = _Seq()
    entries = [_text(next(seq), "t1", "甲"), _text(next(seq), "t1", "乙"), _text(next(seq), "t1", "丙")]

    compacted = compact(entries)

    assert [e.seq for e in compacted] == [1]


# --------------------------------------------------------------------------- #
# 不参与归并的东西
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda s: _tool_call(s, "t1", "echo"), id="工具调用参数"),
        pytest.param(lambda s: _tool_result(s, "t1", "echo"), id="工具结果"),
        pytest.param(
            lambda s: _entry(s, MessagesEvent(task_id="t1", chunk={"content": "", "type": "AIMessageChunk"})),
            id="空正文",
        ),
        pytest.param(lambda s: _entry(s, TaskEndEvent(task_id="t1", status="done", output="完")), id="task_end"),
        pytest.param(lambda s: _entry(s, TaskStartEvent(task_id="t1", description="d")), id="task_start"),
        pytest.param(
            lambda s: _entry(s, SummaryEvent(status="completed", results=[])),
            id="summary",
        ),
    ],
)
def test_non_compactable_events_are_untouched(make):
    seq = _Seq()
    entry = make(next(seq))

    compacted = compact([entry, entry])

    assert len(compacted) == 2, "不可归并的事件必须一条一条留着"
    assert compacted[0] is entry, "连对象都不该被换掉——tee 要把原事件透传给 SSE"


def test_merge_key_is_none_for_non_compactable():
    assert merge_key(TaskEndEvent(task_id="t1", status="done")) is None
    assert merge_key(_tool_call(1, "t1", "echo").event) is None
    assert merge_key(_text(1, "t1", "甲").event) == (
        "t1",
        json.dumps(
            {"langgraph_step": 7, "langgraph_node": "model", "thread_id": "run:t1"},
            sort_keys=True,
            ensure_ascii=False,
        ),
    )


# --------------------------------------------------------------------------- #
# 纯净性：tee 必须能把原事件原样传给下游
# --------------------------------------------------------------------------- #


def test_compaction_does_not_mutate_its_inputs():
    seq = _Seq()
    first, second = _text(next(seq), "t1", "甲"), _text(next(seq), "t1", "乙")

    compact([first, second])

    assert first.event.chunk["content"] == "甲", "入参被改写的话，SSE 会收到被归并过的帧"
    assert second.event.chunk["content"] == "乙"


# --------------------------------------------------------------------------- #
# 累加器与批量版等价
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "content",
    ["甲", "乙", "丙", "丁", "戊"],
)
def test_incremental_accumulator_matches_batch_version(content):
    """``RunJournal`` 走累加器（每条 O(1)），批量版只是它的包装——两者必须一致。"""
    seq = _Seq()
    entries = [_text(next(seq), "t1", c) for c in "甲乙丙丁戊"] + [_tool_call(next(seq), "t1", "echo")]

    compactor = MessageCompactor()
    for entry in entries:
        compactor.add(entry)

    assert compactor.entries == compact(entries)
    assert compactor.entries[0].event.chunk["content"] == "甲乙丙丁戊"
