"""SSE 帧格式化与 RunEvent 映射契约测试。"""

import asyncio
import json

import pytest

from app.gateway.sse import format_sse, map_run_event, sse_consumer
from evaharness.runtime import (
    MessagesEvent,
    PlanEvent,
    PlanTask,
    ReplanEvent,
    RunStartEvent,
    SummaryEvent,
    TaskEndEvent,
    TaskResult,
    TaskStartEvent,
)


def test_format_sse_null_data():
    assert format_sse("end", None) == "event: end\ndata: null\n\n"


def test_format_sse_json_data():
    assert format_sse("messages", {"content": "Hi"}) == 'event: messages\ndata: {"content": "Hi"}\n\n'


def test_format_sse_keeps_unicode():
    assert format_sse("messages", {"content": "你好"}) == 'event: messages\ndata: {"content": "你好"}\n\n'


def test_format_sse_with_event_id():
    assert format_sse("end", None, event_id="abc-123") == "event: end\ndata: null\nid: abc-123\n\n"


# --------------------------------------------------------------------------- #
# map_run_event
# --------------------------------------------------------------------------- #


def _parse(frame):
    """解析单个 SSE 帧为 (event, data)。"""
    event = None
    data = None
    for line in frame.strip("\n").split("\n"):
        if line.startswith("event: "):
            event = line[len("event: ") :]
        elif line.startswith("data: "):
            data = json.loads(line[len("data: ") :])
    return event, data


def test_map_run_event_messages_keeps_t1_wire_shape():
    """data 仍是 [chunk, metadata] 二元数组——T1 的前端解析不用改。

    ``task_id`` 并进 metadata 而不是新增第三个元素：langgraph 本来就把
    「这条 chunk 的附属信息」放在 metadata 里，加一个自有键是同一精神，
    而线上形状一字未动。
    """
    chunk = {"content": "你", "type": "AIMessageChunk"}
    frame = map_run_event(MessagesEvent(task_id="t1", chunk=chunk, metadata={"langgraph_node": "model"}))

    assert _parse(frame) == ("messages", [chunk, {"langgraph_node": "model", "task_id": "t1"}])


def test_map_run_event_run_start_frame():
    event = RunStartEvent(
        run_id="run-abc",
        thread_id="th-xyz",
        request="现在几点？",
        started_at="2026-09-29T10:00:00+00:00",
    )
    name, data = _parse(map_run_event(event))

    assert name == "run_start"
    assert data["thread_id"] == "th-xyz"
    assert data["request"] == "现在几点？"


def test_map_run_event_plan_frame():
    event = PlanEvent(tasks=[PlanTask(id="t1", description="查时间", completion_criteria=["给出时间"])])
    name, data = _parse(map_run_event(event))

    assert name == "plan"
    assert data["type"] == "plan"
    assert data["category"] == "trace"
    assert data["tasks"][0]["id"] == "t1"


def test_map_run_event_task_start_and_end_frames():
    assert _parse(map_run_event(TaskStartEvent(task_id="t1", description="d")))[0] == "task_start"
    name, data = _parse(map_run_event(TaskEndEvent(task_id="t1", status="failed", error="boom")))
    assert name == "task_end"
    assert data["status"] == "failed"
    assert data["error"] == "boom"


def test_map_run_event_replan_frame():
    name, data = _parse(map_run_event(ReplanEvent(should_continue=False, reason="请求已满足")))

    assert name == "replan"
    assert data["should_continue"] is False
    assert data["reason"] == "请求已满足"


def test_map_run_event_summary_frame_is_structured():
    event = SummaryEvent(
        status="partial",
        results=[TaskResult(task_id="t1", status="failed", error="boom")],
        failed=["t1"],
    )
    name, data = _parse(map_run_event(event))

    assert name == "summary"
    assert data["status"] == "partial"
    assert data["failed"] == ["t1"]
    assert data["results"][0]["task_id"] == "t1"


# --------------------------------------------------------------------------- #
# sse_consumer
# --------------------------------------------------------------------------- #


async def _drain(events):
    return [frame async for frame in sse_consumer(events)]


async def _from_list(events, *, error=None):
    for event in events:
        yield event
    if error is not None:
        raise error


@pytest.mark.asyncio
async def test_sse_consumer_maps_events_then_ends():
    events = [
        RunStartEvent(run_id="r", thread_id="t", request="hi", started_at="2026-09-29T10:00:00+00:00"),
        PlanEvent(tasks=[PlanTask(id="t1", description="d")]),
        TaskStartEvent(task_id="t1", description="d"),
        MessagesEvent(task_id="t1", chunk={"content": "hi"}),
        TaskEndEvent(task_id="t1", status="done", output="hi"),
        SummaryEvent(status="completed", results=[TaskResult(task_id="t1", status="done")]),
    ]

    frames = await _drain(_from_list(events))

    assert [_parse(f)[0] for f in frames] == [
        "run_start",
        "plan",
        "task_start",
        "messages",
        "task_end",
        "summary",
        "end",
    ]
    assert _parse(frames[-1]) == ("end", None)


@pytest.mark.asyncio
async def test_sse_consumer_emits_error_frame_on_exception():
    """任何异常都要翻成 error 帧——这里用 ValueError 是因为 PlanValidationError 随下线没了。"""
    frames = await _drain(_from_list([], error=ValueError("坏输入")))

    assert [_parse(f)[0] for f in frames] == ["error", "end"]
    _, data = _parse(frames[0])
    assert data["name"] == "ValueError"
    assert "坏输入" in data["message"]


@pytest.mark.asyncio
async def test_sse_consumer_does_not_swallow_cancellation():
    """硬取消（ADR-0005）必须穿过这一层——它继承 BaseException，不该变成 error 帧。"""

    async def cancelled():
        yield PlanEvent(tasks=[])
        raise asyncio.CancelledError

    frames = []
    with pytest.raises(asyncio.CancelledError):
        async for frame in sse_consumer(cancelled()):
            frames.append(frame)

    assert [_parse(f)[0] for f in frames] == ["plan"]


@pytest.mark.asyncio
async def test_closing_the_consumer_closes_the_upstream_generator():
    """`aclosing` 链：消费者被关掉时，上游生成器也必须被 `aclose`。

    与 MCP 无关——它保的是 `run_agent` **自己的收尾**：runner 的 `finally` 要取消内层 pump
    并注销 run，而那个 `finally` 只有上游被 `aclose` 时才会跑。`async for` 提前退出**不会**自动关
    上游生成器（Python 语义），所以 `sse_consumer` 与 `journaled` 各套了一层 `aclosing`。

    客户端断开时走的就是这条路：`CancelledError` 从最深处穿透，链子逐层收尾。
    """
    closed: list[str] = []

    async def upstream():
        try:
            while True:
                yield RunStartEvent(run_id="r", thread_id="t", request="hi", started_at="2026-10-10T10:00:00+00:00")
        finally:
            closed.append("upstream")

    frames = sse_consumer(upstream())
    await anext(frames)
    assert closed == [], "还没关就不该关上游"

    await frames.aclose()
    assert closed == ["upstream"], "消费者被关掉时上游没被 aclose —— `aclosing` 丢了"
