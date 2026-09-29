"""契约测试：RunEvent 判别联合的序列化与解析。

线上契约是「RunEvent -> SSE 帧」，所以这里钉死的是**观众能看到什么**：每个变体
dump 出来必须带 ``type`` 判别键、必须能 JSON 序列化、必须能原样 parse 回来。
"""

import json

import pytest
from pydantic import ValidationError

from evaharness.agents.plan_exec.events import (
    MessagesEvent,
    PlanEvent,
    ReplanEvent,
    RunEventAdapter,
    SummaryEvent,
    TaskEndEvent,
    TaskStartEvent,
)
from evaharness.agents.plan_exec.schemas import PlanTask, TaskResult


def _roundtrip(event):
    """序列化再解析，返回 (原始 dump 字典, 解析回来的对象)。"""
    dumped = event.model_dump(mode="json")
    assert json.dumps(dumped, ensure_ascii=False)  # 必须 JSON 可序列化
    return dumped, RunEventAdapter.validate_python(dumped)


def test_messages_event_roundtrips_with_type_discriminator():
    event = MessagesEvent(
        task_id="t1",
        chunk={"content": "你", "type": "AIMessageChunk"},
        metadata={"langgraph_node": "model"},
    )
    dumped, parsed = _roundtrip(event)
    assert dumped["type"] == "messages"
    assert dumped["task_id"] == "t1"
    assert parsed == event


def test_plan_event_roundtrips():
    event = PlanEvent(
        run_id="run-abc",
        tasks=[PlanTask(id="t1", description="查时间", completion_criteria=["给出 ISO 时间"])],
    )
    dumped, parsed = _roundtrip(event)
    assert dumped["type"] == "plan"
    assert dumped["run_id"] == "run-abc"
    assert parsed.tasks[0].id == "t1"
    assert parsed.tasks[0].completion_criteria == ["给出 ISO 时间"]


def test_task_start_event_roundtrips():
    event = TaskStartEvent(task_id="t2", description="汇总", depends_on=["t1"])
    dumped, parsed = _roundtrip(event)
    assert dumped["type"] == "task_start"
    assert parsed.depends_on == ["t1"]


@pytest.mark.parametrize("status", ["done", "failed", "skipped"])
def test_task_end_event_accepts_every_terminal_status(status):
    event = TaskEndEvent(task_id="t1", status=status, output="结果", error="炸了" if status == "failed" else None)
    dumped, parsed = _roundtrip(event)
    assert dumped["type"] == "task_end"
    assert parsed.status == status


def test_summary_event_roundtrips_with_structured_payload():
    event = SummaryEvent(
        status="partial",
        results=[
            TaskResult(task_id="t1", status="done", output="ok"),
            TaskResult(task_id="t2", status="failed", error="boom"),
        ],
        failed=["t2"],
        skipped=["t3"],
    )
    dumped, parsed = _roundtrip(event)
    assert dumped["type"] == "summary"
    assert parsed.status == "partial"
    assert [r.task_id for r in parsed.results] == ["t1", "t2"]
    assert parsed.failed == ["t2"]


def test_replan_event_roundtrips():
    event = ReplanEvent(should_continue=False, reason="用户请求已满足")
    dumped, parsed = _roundtrip(event)

    assert dumped["type"] == "replan"
    assert parsed.should_continue is False
    assert parsed.reason == "用户请求已满足"


def test_summary_event_defaults_to_empty_lists():
    event = SummaryEvent(status="completed", results=[TaskResult(task_id="t1", status="done")])
    assert event.failed == []
    assert event.skipped == []


def test_unknown_type_is_rejected():
    with pytest.raises(ValidationError):
        RunEventAdapter.validate_python({"type": "nope"})


def test_missing_discriminator_is_rejected():
    with pytest.raises(ValidationError):
        RunEventAdapter.validate_python({"chunk": {}, "metadata": {}})


def test_every_event_exposes_a_type_literal():
    """SSE 事件名直接取自 type，所以每个变体都必须自带它。"""
    for event in [
        MessagesEvent(task_id="t1", chunk={}),
        PlanEvent(run_id="run-abc", tasks=[]),
        TaskStartEvent(task_id="t1", description="d"),
        TaskEndEvent(task_id="t1", status="done"),
        SummaryEvent(status="completed", results=[]),
        ReplanEvent(should_continue=True, reason=""),
    ]:
        assert isinstance(event.type, str) and event.type
