"""契约测试：Plan-Exec 编排主循环。

全部用替身驱动——planner / executor / replanner 都是注入点，这正是选 async 生成器
而非 LangGraph 三节点图的理由之一。这里钉死的是编排的**语义**：谁先跑、谁被跳过、
什么时候问 replanner、失败怎么记账、取消后不再产出事件。
"""

import asyncio

import pytest
from plan_exec_fakes import ExecutorStep, FakeExecutor, FakePlanner, FakeReplanner, collect

from evaharness.agents.plan_exec.orchestrator import run_plan_exec
from evaharness.agents.plan_exec.schemas import Plan, PlanTask, PlanValidationError, ReplanDecision


def _task(task_id, *, depends_on=()):
    return PlanTask(id=task_id, description=f"任务 {task_id}", depends_on=list(depends_on))


def _types(events):
    return [event.type for event in events]


def _by_type(events, event_type):
    return [event for event in events if event.type == event_type]


# --------------------------------------------------------------------------- #
# 主线：计划 → 执行 → 汇总
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_plan_event_is_first():
    plan = Plan(tasks=[_task("t1")])
    events = await collect(run_plan_exec("问时间", planner=FakePlanner(plan), executor=FakeExecutor()))

    assert events[0].type == "plan"
    assert [task.id for task in events[0].tasks] == ["t1"]
    assert events[-1].type == "summary"


@pytest.mark.asyncio
async def test_single_task_plan_runs_exactly_one_execution_and_summarizes():
    """T2 验收主线：单任务计划退化成一次迭代。"""
    plan = Plan(tasks=[_task("t1")])
    executor = FakeExecutor({"t1": ExecutorStep(output="现在是 18:04", messages=("现在", "是 18:04"))})

    events = await collect(run_plan_exec("现在几点", planner=FakePlanner(plan), executor=executor))

    assert _types(events) == ["plan", "task_start", "messages", "messages", "task_end", "summary"]
    assert len(executor.invocations) == 1
    terminal = _by_type(events, "task_end")[0]
    assert terminal.status == "done"
    assert terminal.output == "现在是 18:04"
    assert _by_type(events, "summary")[0].status == "completed"


@pytest.mark.asyncio
async def test_messages_events_are_forwarded_in_arrival_order():
    plan = Plan(tasks=[_task("t1")])
    executor = FakeExecutor({"t1": ExecutorStep(messages=("甲", "乙", "丙"))})

    events = await collect(run_plan_exec("req", planner=FakePlanner(plan), executor=executor))

    assert [event.chunk["content"] for event in _by_type(events, "messages")] == ["甲", "乙", "丙"]


@pytest.mark.asyncio
async def test_task_end_output_lands_in_summary():
    plan = Plan(tasks=[_task("t1")])
    executor = FakeExecutor({"t1": ExecutorStep(output="产物 A")})

    events = await collect(run_plan_exec("req", planner=FakePlanner(plan), executor=executor))

    summary = _by_type(events, "summary")[0]
    assert [(r.task_id, r.status, r.output) for r in summary.results] == [("t1", "done", "产物 A")]


@pytest.mark.asyncio
async def test_executor_invocation_carries_task_run_id_and_dependency_results():
    plan = Plan(tasks=[_task("t1"), _task("t2", depends_on=["t1"])])
    executor = FakeExecutor({"t1": ExecutorStep(output="上游产物"), "t2": ExecutorStep(output="下游产物")})

    await collect(run_plan_exec("req", planner=FakePlanner(plan), executor=executor))

    first, second = executor.invocations
    assert first.task.id == "t1"
    assert first.run_id and first.run_id == second.run_id, "同一次运行必须共用一个 run_id"
    assert first.dependency_results == ()
    assert [(r.task_id, r.output) for r in second.dependency_results] == [("t1", "上游产物")]


@pytest.mark.asyncio
async def test_two_runs_use_distinct_run_ids():
    plan = Plan(tasks=[_task("t1")])
    executor = FakeExecutor()
    planner = FakePlanner(plan)

    await collect(run_plan_exec("req", planner=planner, executor=executor))
    await collect(run_plan_exec("req", planner=planner, executor=executor))

    assert executor.invocations[0].run_id != executor.invocations[1].run_id


# --------------------------------------------------------------------------- #
# 顺序与依赖
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dependent_tasks_run_in_dependency_order():
    """计划书里 t1 写在 t2 后面，但 t2 依赖 t1，所以 t1 必须先跑。"""
    plan = Plan(tasks=[_task("t2", depends_on=["t1"]), _task("t1")])
    executor = FakeExecutor()

    events = await collect(run_plan_exec("req", planner=FakePlanner(plan), executor=executor))

    starts = [event.task_id for event in _by_type(events, "task_start")]
    assert starts == ["t1", "t2"]


@pytest.mark.asyncio
async def test_independent_tasks_run_in_plan_order():
    plan = Plan(tasks=[_task("t3"), _task("t1"), _task("t2")])
    executor = FakeExecutor()

    events = await collect(run_plan_exec("req", planner=FakePlanner(plan), executor=executor))

    assert [event.task_id for event in _by_type(events, "task_start")] == ["t3", "t1", "t2"]


@pytest.mark.asyncio
async def test_dangling_dependency_task_is_skipped():
    """悬空依赖永远不 ready，收尾时自然变 skipped——不需要特殊代码。"""
    plan = Plan(tasks=[_task("t1"), _task("t2", depends_on=["ghost"])])
    executor = FakeExecutor()

    events = await collect(run_plan_exec("req", planner=FakePlanner(plan), executor=executor))

    assert [inv.task.id for inv in executor.invocations] == ["t1"]
    summary = _by_type(events, "summary")[0]
    assert summary.skipped == ["t2"]
    assert summary.status == "partial"


# --------------------------------------------------------------------------- #
# 失败：best-effort（ADR-0005）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_failed_task_marks_failed_and_other_branch_still_runs():
    plan = Plan(tasks=[_task("t1"), _task("t2")])
    executor = FakeExecutor(
        {
            "t1": ExecutorStep(status="failed", output=None, error="工具炸了"),
            "t2": ExecutorStep(output="我照跑"),
        }
    )

    events = await collect(run_plan_exec("req", planner=FakePlanner(plan), executor=executor))

    assert [inv.task.id for inv in executor.invocations] == ["t1", "t2"]
    summary = _by_type(events, "summary")[0]
    assert summary.status == "partial"
    assert summary.failed == ["t1"]
    assert dict((r.task_id, r.status) for r in summary.results) == {"t1": "failed", "t2": "done"}


@pytest.mark.asyncio
async def test_downstream_of_failed_task_is_skipped():
    plan = Plan(tasks=[_task("t1"), _task("t2", depends_on=["t1"]), _task("t3")])
    executor = FakeExecutor({"t1": ExecutorStep(status="failed", output=None, error="boom")})

    events = await collect(run_plan_exec("req", planner=FakePlanner(plan), executor=executor))

    assert [inv.task.id for inv in executor.invocations] == ["t1", "t3"]
    summary = _by_type(events, "summary")[0]
    assert summary.skipped == ["t2"]
    assert summary.failed == ["t1"]


@pytest.mark.asyncio
async def test_executor_without_terminal_event_is_recorded_failed():
    """执行单元中途没了（没发终态也没抛）——补一条 failed，不能让它静默消失。"""
    plan = Plan(tasks=[_task("t1")])
    executor = FakeExecutor({"t1": ExecutorStep(emit_terminal=False, messages=("半截",))})

    events = await collect(run_plan_exec("req", planner=FakePlanner(plan), executor=executor))

    terminal = _by_type(events, "task_end")
    assert len(terminal) == 1
    assert terminal[0].status == "failed"
    assert _by_type(events, "summary")[0].status == "partial"


# --------------------------------------------------------------------------- #
# replanner：只判继续/结束
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_replanner_not_called_when_no_ready_task_remains():
    """单任务计划零次 replanner 调用——不为一个必然的答案付一次 LLM 调用。"""
    plan = Plan(tasks=[_task("t1")])
    replanner = FakeReplanner()

    await collect(run_plan_exec("req", planner=FakePlanner(plan), executor=FakeExecutor(), replanner=replanner))

    assert replanner.calls == []


@pytest.mark.asyncio
async def test_replanner_not_called_when_remaining_tasks_are_blocked():
    plan = Plan(tasks=[_task("t1"), _task("t2", depends_on=["t1"])])
    replanner = FakeReplanner()
    executor = FakeExecutor({"t1": ExecutorStep(status="failed", output=None, error="boom")})

    await collect(run_plan_exec("req", planner=FakePlanner(plan), executor=executor, replanner=replanner))

    assert replanner.calls == [], "剩下的任务被失败阻塞，问了也只能得到「没必要继续」"


@pytest.mark.asyncio
async def test_replanner_continue_keeps_loop_going():
    plan = Plan(tasks=[_task("t1"), _task("t2")])
    replanner = FakeReplanner([ReplanDecision(should_continue=True, reason="还有正事")])
    executor = FakeExecutor()

    events = await collect(run_plan_exec("req", planner=FakePlanner(plan), executor=executor, replanner=replanner))

    assert len(replanner.calls) == 1
    assert [inv.task.id for inv in executor.invocations] == ["t1", "t2"]
    assert _by_type(events, "summary")[0].status == "completed"


@pytest.mark.asyncio
async def test_replanner_sees_results_so_far():
    plan = Plan(tasks=[_task("t1"), _task("t2")])
    replanner = FakeReplanner([ReplanDecision(should_continue=True)])
    executor = FakeExecutor({"t1": ExecutorStep(output="第一段的产物")})

    await collect(run_plan_exec("原始请求", planner=FakePlanner(plan), executor=executor, replanner=replanner))

    request, results = replanner.calls[0]
    assert request == "原始请求"
    assert [(r.task_id, r.output) for r in results] == [("t1", "第一段的产物")]


@pytest.mark.asyncio
async def test_replan_event_surfaces_the_decision():
    """replanner 的判定必须可见——否则用户只看到「已跳过」，不知道为什么。"""
    plan = Plan(tasks=[_task("t1"), _task("t2")])
    replanner = FakeReplanner([ReplanDecision(should_continue=True, reason="还有正事")])

    events = await collect(
        run_plan_exec("req", planner=FakePlanner(plan), executor=FakeExecutor(), replanner=replanner)
    )

    replans = _by_type(events, "replan")
    assert len(replans) == 1
    assert replans[0].should_continue is True
    assert replans[0].reason == "还有正事"


@pytest.mark.asyncio
async def test_replan_event_precedes_the_next_task():
    plan = Plan(tasks=[_task("t1"), _task("t2")])
    replanner = FakeReplanner([ReplanDecision(should_continue=True)])

    events = await collect(
        run_plan_exec("req", planner=FakePlanner(plan), executor=FakeExecutor(), replanner=replanner)
    )

    names = _types(events)
    assert names.index("replan") < names.index("task_start", names.index("task_start") + 1)


@pytest.mark.asyncio
async def test_no_replan_event_when_replanner_is_not_consulted():
    plan = Plan(tasks=[_task("t1")])

    events = await collect(
        run_plan_exec("req", planner=FakePlanner(plan), executor=FakeExecutor(), replanner=FakeReplanner())
    )

    assert _by_type(events, "replan") == []


@pytest.mark.asyncio
async def test_replanner_stop_skips_remaining_and_status_is_stopped():
    plan = Plan(tasks=[_task("t1"), _task("t2"), _task("t3")])
    replanner = FakeReplanner([ReplanDecision(should_continue=False, reason="已经够了")])
    executor = FakeExecutor()

    events = await collect(run_plan_exec("req", planner=FakePlanner(plan), executor=executor, replanner=replanner))

    assert [inv.task.id for inv in executor.invocations] == ["t1"]
    summary = _by_type(events, "summary")[0]
    assert summary.status == "stopped"
    assert summary.skipped == ["t2", "t3"]
    assert [event.task_id for event in _by_type(events, "task_end")] == ["t1", "t2", "t3"]
    assert _by_type(events, "replan")[0].reason == "已经够了"


# --------------------------------------------------------------------------- #
# 汇总的完整性
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_summary_covers_every_task_in_plan_order():
    plan = Plan(tasks=[_task("t3"), _task("t1"), _task("t2")])
    events = await collect(run_plan_exec("req", planner=FakePlanner(plan), executor=FakeExecutor()))

    summary = _by_type(events, "summary")[0]
    assert [r.task_id for r in summary.results] == ["t3", "t1", "t2"]


@pytest.mark.asyncio
async def test_skipped_tasks_get_explicit_task_end_events():
    """面板靠 task_end 收尾；跳过也得发，否则前端卡片永远停在 pending。"""
    plan = Plan(tasks=[_task("t1"), _task("t2", depends_on=["ghost"])])
    events = await collect(run_plan_exec("req", planner=FakePlanner(plan), executor=FakeExecutor()))

    skipped = [event for event in _by_type(events, "task_end") if event.status == "skipped"]
    assert [event.task_id for event in skipped] == ["t2"]


# --------------------------------------------------------------------------- #
# 计划非法 / planner 失败
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_invalid_plan_emits_plan_event_then_raises():
    """坏计划也要先让前端看见——不然用户只看到一句报错，不知道 planner 想干什么。"""
    plan = Plan(tasks=[_task("t1", depends_on=["t1"])])
    stream = run_plan_exec("req", planner=FakePlanner(plan), executor=FakeExecutor())

    seen = []
    with pytest.raises(PlanValidationError):
        async for event in stream:
            seen.append(event)

    assert [event.type for event in seen] == ["plan"]


@pytest.mark.asyncio
async def test_planner_exception_propagates_without_plan_event():
    planner = FakePlanner(error=RuntimeError("planner 挂了"))
    stream = run_plan_exec("req", planner=planner, executor=FakeExecutor())

    seen = []
    with pytest.raises(RuntimeError, match="planner 挂了"):
        async for event in stream:
            seen.append(event)

    assert seen == []


# --------------------------------------------------------------------------- #
# 取消（ADR-0005 硬取消）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cancellation_propagates_and_no_summary():
    plan = Plan(tasks=[_task("t1"), _task("t2")])
    executor = FakeExecutor(default=ExecutorStep(hangs=True))
    emitted = []

    async def consume():
        async for event in run_plan_exec("req", planner=FakePlanner(plan), executor=executor):
            emitted.append(event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert executor.saw_cancellation, "取消必须穿透到执行单元内部正在 await 的地方"
    assert not _by_type(emitted, "summary"), "取消后不再产出事件（清理路径里 yield 会炸）"
