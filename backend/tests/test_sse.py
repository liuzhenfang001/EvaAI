"""SSE 帧格式化与 RunEvent 映射契约测试。"""

import asyncio
import json

import pytest

from app.gateway.sse import format_sse, map_run_event, sse_consumer
from evaharness.agents.plan_exec.events import (
    MessagesEvent,
    PlanEvent,
    ReplanEvent,
    SummaryEvent,
    TaskEndEvent,
    TaskStartEvent,
)
from evaharness.agents.plan_exec.schemas import PlanTask, PlanValidationError, TaskResult


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
    """data 仍是 [chunk, metadata] 二元数组——T1 的前端解析不用改。"""
    chunk = {"content": "你", "type": "AIMessageChunk"}
    frame = map_run_event(MessagesEvent(chunk=chunk, metadata={"langgraph_node": "model"}))

    assert _parse(frame) == ("messages", [chunk, {"langgraph_node": "model"}])


def test_map_run_event_plan_frame():
    event = PlanEvent(tasks=[PlanTask(id="t1", description="查时间", completion_criteria=["给出时间"])])
    name, data = _parse(map_run_event(event))

    assert name == "plan"
    assert data["type"] == "plan"
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
        PlanEvent(tasks=[PlanTask(id="t1", description="d")]),
        TaskStartEvent(task_id="t1", description="d"),
        MessagesEvent(chunk={"content": "hi"}),
        TaskEndEvent(task_id="t1", status="done", output="hi"),
        SummaryEvent(status="completed", results=[TaskResult(task_id="t1", status="done")]),
    ]

    frames = await _drain(_from_list(events))

    assert [_parse(f)[0] for f in frames] == ["plan", "task_start", "messages", "task_end", "summary", "end"]
    assert _parse(frames[-1]) == ("end", None)


@pytest.mark.asyncio
async def test_sse_consumer_emits_error_frame_on_exception():
    frames = await _drain(_from_list([], error=PlanValidationError("坏计划")))

    assert [_parse(f)[0] for f in frames] == ["error", "end"]
    _, data = _parse(frames[0])
    assert data["name"] == "PlanValidationError"
    assert "坏计划" in data["message"]


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
