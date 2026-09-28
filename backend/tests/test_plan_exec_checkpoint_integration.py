"""契约测试：执行单元与 checkpointer 的接线。

``thread_id`` 有两套投递通道，用途不同、缺一不可：

- ``config={"configurable": {"thread_id": ...}}`` —— checkpointer 从这里取键，**缺了直接 ValueError**；
- ``context={"thread_id": ...}`` —— ``LoopDetectionMiddleware`` 从 ``runtime.context`` 取键，
  用于把「重复工具调用」的滑动窗口按线程隔离。

只传其中一个都会出问题（前者报错、后者让多个任务共用同一个滑窗），所以两条都钉住。
"""

import itertools

import pytest
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from plan_exec_fakes import ToolCapableFake, collect

from evaharness.agents.plan_exec.executor import TaskInvocation, make_react_executor
from evaharness.agents.plan_exec.schemas import PlanTask
from evaharness.agents.react import make_react_agent


def _task(task_id: str) -> PlanTask:
    return PlanTask(id=task_id, description=f"任务 {task_id}", completion_criteria=["完成"])


def _model() -> ToolCapableFake:
    """回复无限循环：跑多个任务时不能把剧本用尽。"""
    return ToolCapableFake(messages=itertools.cycle([AIMessage(content="答案")]))


def _invocation(task_id: str, run_id: str = "run-1") -> TaskInvocation:
    return TaskInvocation(task=_task(task_id), dependency_results=(), run_id=run_id)


@pytest.mark.asyncio
async def test_state_is_readable_from_the_checkpointer_after_a_task():
    saver = InMemorySaver()
    executor = make_react_executor(_model(), checkpointer=saver)

    await collect(executor(_invocation("t1")))

    state = await saver.aget({"configurable": {"thread_id": "run-1:t1"}})
    assert state is not None
    contents = [m.content for m in state["channel_values"]["messages"]]
    assert "答案" in contents


@pytest.mark.asyncio
async def test_tasks_do_not_share_checkpoint_state():
    saver = InMemorySaver()
    executor = make_react_executor(_model(), checkpointer=saver)

    await collect(executor(_invocation("t1")))
    await collect(executor(_invocation("t2")))

    threads = {tup.config["configurable"]["thread_id"] async for tup in saver.alist(None)}
    assert threads == {"run-1:t1", "run-1:t2"}


@pytest.mark.asyncio
async def test_checkpointer_requires_thread_id_in_config_not_just_context():
    """钉住上游行为：接上 checkpointer 后，只给 ``context`` 会直接报错。

    执行单元两个都传（见 executor.py）。这条测试保证「只传 context」这个简化
    一旦被写进代码就会立刻红——而不是等到某个任务跑起来才炸。
    """
    graph = make_react_agent(_model(), checkpointer=InMemorySaver())

    with pytest.raises(ValueError, match="thread_id"):
        async for _ in graph.astream(
            {"messages": [("user", "hi")]},
            stream_mode="values",
            context={"thread_id": "run-1:t1"},
        ):
            pass


@pytest.mark.asyncio
async def test_executor_forwards_a_per_task_context_to_middleware():
    """执行单元负责把 ``run_id:task_id`` 送进 ``runtime.context``。

    ``LoopDetectionMiddleware`` 用它给「重复工具调用」的滑窗分桶，共用桶会累加到
    硬上限、把正常的工具调用误判成循环。分桶本身由 test_loop_detection.py 覆盖，
    这里只钉住我们这一侧：每次派发都送了、且是本次运行的 id。
    """

    class ContextProbe(AgentMiddleware):
        def __init__(self) -> None:
            super().__init__()
            self.seen: list[str] = []

        def after_model(self, state, runtime):  # noqa: ANN001, ANN201
            context = getattr(runtime, "context", None)
            if isinstance(context, dict) and context.get("thread_id"):
                self.seen.append(str(context["thread_id"]))
            return None

    probe = ContextProbe()
    executor = make_react_executor(_model(), middleware=[probe])

    await collect(executor(_invocation("t1", run_id="run-1")))
    await collect(executor(_invocation("t2", run_id="run-1")))
    await collect(executor(_invocation("t1", run_id="run-2")))

    assert probe.seen == ["run-1:t1", "run-1:t2", "run-2:t1"]
