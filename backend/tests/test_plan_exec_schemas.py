"""契约测试：Plan-Exec 的计划 schema 与校验。"""

import pytest
from pydantic import ValidationError

from evaharness.agents.plan_exec.schemas import (
    Plan,
    PlanTask,
    PlanValidationError,
    TaskResult,
    validate_plan,
)


def _task(task_id, *, depends_on=(), criteria=("完成",)):
    return PlanTask(
        id=task_id,
        description=f"任务 {task_id}",
        completion_criteria=list(criteria),
        depends_on=list(depends_on),
    )


def test_plan_task_defaults():
    task = PlanTask(id="t1", description="查时间")
    assert task.completion_criteria == []
    assert task.depends_on == []


def test_accepts_valid_dag():
    plan = Plan(tasks=[_task("t1"), _task("t2", depends_on=["t1"]), _task("t3", depends_on=["t1", "t2"])])
    validate_plan(plan)  # 不抛即通过


def test_rejects_empty_plan():
    with pytest.raises(PlanValidationError, match="at least one task"):
        validate_plan(Plan(tasks=[]))


def test_rejects_duplicate_ids():
    with pytest.raises(PlanValidationError, match="duplicate"):
        validate_plan(Plan(tasks=[_task("t1"), _task("t1")]))


def test_rejects_self_dependency():
    with pytest.raises(PlanValidationError, match="depend on itself"):
        validate_plan(Plan(tasks=[_task("t1", depends_on=["t1"])]))


def test_rejects_cycle():
    with pytest.raises(PlanValidationError, match="cycle"):
        validate_plan(Plan(tasks=[_task("t1", depends_on=["t2"]), _task("t2", depends_on=["t1"])]))


def test_rejects_longer_cycle():
    plan = Plan(
        tasks=[
            _task("t1", depends_on=["t3"]),
            _task("t2", depends_on=["t1"]),
            _task("t3", depends_on=["t2"]),
        ]
    )
    with pytest.raises(PlanValidationError, match="cycle"):
        validate_plan(plan)


def test_dangling_dependency_is_not_a_validation_error():
    """悬空依赖是 LLM 幻觉噪音，交给「永不 ready → skipped」自然处理，不在此拦截。"""
    validate_plan(Plan(tasks=[_task("t1", depends_on=["ghost"])]))


def test_task_result_requires_terminal_status():
    with pytest.raises(ValidationError):
        TaskResult(task_id="t1", status="running")


@pytest.mark.parametrize("status", ["done", "failed", "skipped"])
def test_task_result_accepts_terminal_statuses(status):
    assert TaskResult(task_id="t1", status=status).status == status
