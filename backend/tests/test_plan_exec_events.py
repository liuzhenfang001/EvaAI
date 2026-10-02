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
    RunEndEvent,
    RunEventAdapter,
    RunStartEvent,
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


def test_run_start_event_roundtrips():
    """``run_start`` 是每个 run 的首帧，承载 run 身份与用户原始请求。"""
    event = RunStartEvent(
        run_id="run-abc",
        thread_id="th-xyz",
        request="现在几点？",
        started_at="2026-09-29T10:00:00+00:00",
    )
    dumped, parsed = _roundtrip(event)
    assert dumped["type"] == "run_start"
    assert dumped["thread_id"] == "th-xyz"
    assert parsed.request == "现在几点？"


def test_run_start_mode_defaults_to_plan_for_pre_t8_journals():
    """``mode`` 的缺省值是**历史回退**，不是语义缺省。

    T7 期间写下的 journal 行里没有这个字段，而那些行确实都是 Plan-Exec 写的，
    所以解析时必须回退成 ``plan``——否则旧会话会整段渲染错（署名、面板文案、
    「规划中…」占位都由它决定）。
    """
    dumped, parsed = _roundtrip(
        RunStartEvent(run_id="r", thread_id="t", request="q", started_at="2026-09-29T10:00:00+00:00")
    )
    assert dumped["mode"] == "plan"
    assert parsed.mode == "plan"


def test_run_start_mode_carries_the_react_path():
    _, parsed = _roundtrip(
        RunStartEvent(run_id="r", thread_id="t", request="q", started_at="2026-09-30T10:00:00+00:00", mode="react")
    )
    assert parsed.mode == "react"


def test_run_start_rejects_an_unknown_mode():
    with pytest.raises(ValidationError):
        RunStartEvent(run_id="r", thread_id="t", request="q", started_at="s", mode="nope")


def test_run_end_event_roundtrips():
    """``run_end`` 是 lead 路径的终态：它没有 ``task_end``，message 桶里必须有个收尾。"""
    event = RunEndEvent(status="completed", output="现在 18:04")
    dumped, parsed = _roundtrip(event)

    assert dumped["type"] == "run_end"
    assert parsed.status == "completed"
    assert parsed.output == "现在 18:04"
    assert parsed.error is None


def test_run_end_event_carries_failure_detail():
    event = RunEndEvent(status="failed", error="RuntimeError: boom")
    dumped, parsed = _roundtrip(event)

    assert dumped["type"] == "run_end"
    assert parsed.status == "failed"
    assert parsed.error == "RuntimeError: boom"
    assert parsed.output is None


def test_plan_event_roundtrips():
    """``plan`` 只管计划内容——run 身份自 T7 起由 ``run_start`` 承载。"""
    event = PlanEvent(tasks=[PlanTask(id="t1", description="查时间", completion_criteria=["给出 ISO 时间"])])
    dumped, parsed = _roundtrip(event)

    assert dumped["type"] == "plan"
    assert "run_id" not in dumped, "run_id 已迁到 run_start，不该在 plan 上留影子"
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


def _all_events() -> list:
    return [
        RunStartEvent(run_id="r", thread_id="t", request="q", started_at="2026-09-29T10:00:00+00:00"),
        MessagesEvent(task_id="t1", chunk={}),
        PlanEvent(tasks=[]),
        TaskStartEvent(task_id="t1", description="d"),
        TaskEndEvent(task_id="t1", status="done"),
        SummaryEvent(status="completed", results=[]),
        ReplanEvent(should_continue=True, reason=""),
        RunEndEvent(status="completed", output="好了"),
    ]


def test_every_event_exposes_a_type_literal():
    """SSE 事件名直接取自 type，所以每个变体都必须自带它。"""
    for event in _all_events():
        assert isinstance(event.type, str) and event.type


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        ("run_start", "message"),
        ("messages", "message"),
        ("task_end", "message"),
        ("run_end", "message"),
        ("plan", "trace"),
        ("task_start", "trace"),
        ("summary", "trace"),
        ("replan", "trace"),
    ],
)
def test_every_event_declares_a_routing_category(event_type, expected):
    """``category`` 是给消费者路由用的桶（ADR-0009）：

    ``message`` 桶承载对话内容（用户请求、助手正文、任务产出与成败），
    ``trace`` 桶只讲编排过程。对话投影过滤 ``message``；trace 投影取**全部**。
    """
    event = next(e for e in _all_events() if e.type == event_type)

    assert event.category == expected
    assert event.model_dump(mode="json")["category"] == expected, "category 必须随事件上线"
