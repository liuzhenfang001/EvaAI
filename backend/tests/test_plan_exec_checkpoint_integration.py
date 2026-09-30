"""契约测试：执行单元与 checkpointer 的接线。

checkpoint 的 thread 坐标是 ``{会话}:{运行}:{任务}``（``task_thread_id``），而它有两套
投递通道，用途不同、缺一不可：

- ``config={"configurable": {"thread_id": ...}}`` —— checkpointer 从这里取键，**缺了直接 ValueError**；
- ``context={"thread_id": ...}`` —— ``LoopDetectionMiddleware`` 从 ``runtime.context`` 取键，
  用于把「重复工具调用」的滑动窗口按线程隔离。

只传其中一个都会出问题（前者报错、后者让多个任务共用同一个滑窗），所以两条都钉住。
"""

import itertools

import pytest
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from plan_exec_fakes import ToolCapableFake, collect

from evaharness.agents.plan_exec.executor import TaskInvocation, make_react_executor
from evaharness.agents.plan_exec.schemas import PlanTask
from evaharness.agents.react import make_react_agent
from evaharness.checkpoint import make_sqlite_checkpointer, task_thread_id

THREAD = "th-1"
"""本文件里所有任务所属的会话——checkpoint 坐标的第一段。"""


def _task(task_id: str) -> PlanTask:
    return PlanTask(id=task_id, description=f"任务 {task_id}", completion_criteria=["完成"])


def _model() -> ToolCapableFake:
    """回复无限循环：跑多个任务时不能把剧本用尽。"""
    return ToolCapableFake(messages=itertools.cycle([AIMessage(content="答案")]))


def _invocation(task_id: str, run_id: str = "run-1") -> TaskInvocation:
    return TaskInvocation(task=_task(task_id), dependency_results=(), run_id=run_id, thread_id=THREAD)


@pytest.mark.asyncio
async def test_state_is_readable_from_the_checkpointer_after_a_task():
    saver = InMemorySaver()
    executor = make_react_executor(_model(), checkpointer=saver)

    await collect(executor(_invocation("t1")))

    state = await saver.aget({"configurable": {"thread_id": f"{THREAD}:run-1:t1"}})
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
    assert threads == {f"{THREAD}:run-1:t1", f"{THREAD}:run-1:t2"}


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
            context={"thread_id": f"{THREAD}:run-1:t1"},
        ):
            pass


@pytest.mark.asyncio
async def test_executor_forwards_a_per_task_context_to_middleware():
    """执行单元负责把 ``会话:run_id:task_id`` 送进 ``runtime.context``。

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

    assert probe.seen == [f"{THREAD}:run-1:t1", f"{THREAD}:run-1:t2", f"{THREAD}:run-2:t1"]


# --------------------------------------------------------------------------- #
# 为什么「有 checkpointer 却没有多轮记忆」
# --------------------------------------------------------------------------- #


class _HistoryRecorder(ToolCapableFake):
    """记录模型这一次到底看见了哪些消息。"""

    seen: list[str] = []

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001, ANN003
        type(self).seen = [f"{type(m).__name__}" for m in messages]
        async for chunk in super()._astream(messages, stop, run_manager, **kwargs):
            yield chunk


@pytest.mark.asyncio
async def test_checkpointer_loads_history_only_when_the_thread_coordinate_repeats(tmp_path):
    """钉住一个容易反复被重新推导的问题：**有 checkpointer 不等于有记忆**。

    checkpointer 确实会自动装载「同一 thread_id 下已有的历史」——这条成立（第二种跑法
    里模型看见了上一轮的消息）。问题在于**我们的坐标每次运行都会换**
    （``{会话}:{运行}:{任务}``），于是每次都开一张空 thread，没有历史可装（第三种跑法）。

    坐标之所以要每次换，是因为它兼任两件事：隔离每个任务的图状态、给
    ``LoopDetectionMiddleware`` 分桶（共用会把正常工具调用误判成循环并剥掉 tool_calls）。
    所以「多轮记忆」对我们**只能显式喂进 prompt**，拿不到白送的。

    参照项目 DeerFlow 能白拿，是因为它的隔离维度是 LangGraph 的**子图命名空间**
    （子 agent 共享父 thread_id），而我们的隔离维度是 **thread**（ADR-0004 / ADR-0006
    选的独立图并行派发）。这是架构差异，不是 API 用法差异。
    """
    async with make_sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        model = _HistoryRecorder(messages=itertools.cycle([AIMessage(content="好的")]))
        graph = make_react_agent(model, [], system_prompt="你是助手", checkpointer=saver)

        async def run(thread_id: str) -> list[str]:
            async for _ in graph.astream(
                {"messages": [HumanMessage(content="问题是？")]},
                config={"configurable": {"thread_id": thread_id}},
                context={"thread_id": thread_id},
                stream_mode=["messages", "values"],
            ):
                pass
            return list(_HistoryRecorder.seen)

        same = task_thread_id(THREAD, "run-1", "t1")
        await run(same)
        first = await run(same)  # 同一坐标：历史被装载
        other = await run(task_thread_id(THREAD, "run-2", "t1"))  # 换一次运行：新坐标

    assert first.count("HumanMessage") == 2, "同一坐标下，上一轮的消息确实被自动装载了"
    assert other.count("HumanMessage") == 1, "换了坐标就是空 thread——这正是我们没有记忆的原因"
