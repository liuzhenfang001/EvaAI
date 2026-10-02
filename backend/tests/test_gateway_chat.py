"""Plan-Exec 的事件流 → SSE 帧契约。

T8 之后这条路径**不再是默认**（退到 ``/chat/plan/stream`` 封存），所以这一组断言
覆盖的是封存路径仍然要守住的契约；默认路径（主 Agent）见 ``test_lead_agent.py``，
两条路由的接线见 ``test_gateway_routes.py``。

planner 与 replanner 用替身（结构化输出无法用假模型驱动），**执行单元是真的**——
这样才覆盖到「事件流 → SSE 帧」这条真实的接缝。
"""

import itertools
import json

import pytest
from langchain_core.messages import AIMessage
from plan_exec_fakes import FakePlanner, ToolCapableFake

from app.gateway.sse import sse_consumer
from evaharness.agents.plan_exec import make_react_executor, run_plan_exec
from evaharness.agents.plan_exec.schemas import Plan, PlanTask, PlanValidationError


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


def _executor(*replies):
    model = ToolCapableFake(messages=itertools.cycle([AIMessage(content=r) for r in replies]))
    return make_react_executor(model)


async def _collect(request, *, plan, replanner=None, replies=("答案",), run_id=None, thread_id=None):
    events = run_plan_exec(
        request,
        planner=FakePlanner(plan),
        executor=_executor(*replies),
        replanner=replanner,
        run_id=run_id,
        thread_id=thread_id,
    )
    return [frame async for frame in sse_consumer(events)]


@pytest.mark.asyncio
async def test_full_chain_emits_plan_messages_task_summary_end():
    plan = Plan(tasks=[PlanTask(id="t1", description="查询当前时间", completion_criteria=["给出 ISO 时间"])])

    frames = await _collect("现在几点？", plan=plan)
    names = [_parse(frame)[0] for frame in frames]

    assert names[0] == "run_start", "首帧是 run 元事件（带回 thread_id 与用户请求）"
    assert names[1] == "plan"
    assert names[-1] == "end"
    for expected in ("task_start", "messages", "task_end", "summary"):
        assert expected in names, f"缺少 {expected} 帧：{names}"


@pytest.mark.asyncio
async def test_answer_text_flows_through_as_streamed_tokens():
    plan = Plan(tasks=[PlanTask(id="t1", description="查时间")])

    frames = await _collect("现在几点？", plan=plan, replies=("现在是 18:04",))
    streamed = "".join(
        chunk["content"]
        for name, data in (_parse(frame) for frame in frames)
        if name == "messages"
        for chunk in [data[0]]
        if chunk.get("type") != "tool"
    )

    assert "现在是 18:04" in streamed


@pytest.mark.asyncio
async def test_run_start_frame_carries_the_run_and_thread_ids():
    """前端靠它认领会话、并把这次运行从 RunJournal 拉回来（回放的凭据）。"""
    plan = Plan(tasks=[PlanTask(id="t1", description="查时间")])

    frames = await _collect("现在几点？", plan=plan, run_id="run-abc", thread_id="th-abc")
    name, data = _parse(frames[0])

    assert name == "run_start"
    assert data["run_id"] == "run-abc"
    assert data["thread_id"] == "th-abc"
    assert data["request"] == "现在几点？"
    assert data["mode"] == "plan", "这条路径必须自报家门为 plan"


@pytest.mark.asyncio
async def test_messages_frame_keeps_t1_wire_shape():
    plan = Plan(tasks=[PlanTask(id="t1", description="查时间")])

    frames = await _collect("现在几点？", plan=plan)
    data = next(data for name, data in (_parse(frame) for frame in frames) if name == "messages")

    assert isinstance(data, list) and len(data) == 2
    assert isinstance(data[0], dict) and "content" in data[0]
    assert isinstance(data[1], dict)
    assert data[1]["task_id"] == "t1", "并行后 token 交错，归属只能靠这个键"


@pytest.mark.asyncio
async def test_plan_frame_carries_the_structured_plan():
    plan = Plan(
        tasks=[
            PlanTask(id="t1", description="查时间", completion_criteria=["给出 ISO 时间"]),
            PlanTask(id="t2", description="算剩余小时", depends_on=["t1"]),
        ]
    )

    frames = await _collect("先查时间再算剩余小时", plan=plan)
    _, data = next((n, d) for n, d in (_parse(f) for f in frames) if n == "plan")

    assert [task["id"] for task in data["tasks"]] == ["t1", "t2"]
    assert data["tasks"][1]["depends_on"] == ["t1"]
    assert data["tasks"][0]["completion_criteria"] == ["给出 ISO 时间"]


@pytest.mark.asyncio
async def test_summary_frame_reports_failed_tasks():
    plan = Plan(tasks=[PlanTask(id="t1", description="查时间")])

    frames = await _collect(
        "现在几点？",
        plan=plan,
        replies=("",),
    )
    name, data = next((n, d) for n, d in (_parse(frame) for frame in frames) if n == "summary")

    assert name == "summary"
    assert data["status"] == "completed"
    assert data["results"][0]["task_id"] == "t1"


@pytest.mark.asyncio
async def test_invalid_plan_yields_error_frame_after_plan_frame():
    plan = Plan(tasks=[PlanTask(id="t1", description="查时间", depends_on=["t1"])])

    frames = await _collect("现在几点？", plan=plan)
    names = [_parse(frame)[0] for frame in frames]

    assert names == ["run_start", "plan", "error", "end"]
    _, data = _parse(frames[2])
    assert data["name"] == PlanValidationError.__name__
