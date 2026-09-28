"""契约测试：ReAct 任务执行单元。

执行单元是「计划里的一个任务」到「ReAct 图的一次运行」之间的适配层：它把 token
流翻译成 MessagesEvent，把终态翻译成 TaskEndEvent，把异常翻译成失败终态（而不是
让整个运行炸掉）。
"""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from plan_exec_fakes import RecordingFake, ToolCapableFake, collect, echo

from evaharness.agents.plan_exec.executor import (
    TaskInvocation,
    final_text,
    make_react_executor,
    render_task_prompt,
)
from evaharness.agents.plan_exec.schemas import PlanTask, TaskResult


def _task(**overrides):
    base = {"id": "t1", "description": "查询当前时间", "completion_criteria": ["给出 ISO 8601 时间"], "depends_on": []}
    return PlanTask(**{**base, **overrides})


def _invocation(task=None, deps=(), run_id="run-abc"):
    return TaskInvocation(task=task or _task(), dependency_results=tuple(deps), run_id=run_id)


def _model(*responses):
    return ToolCapableFake(messages=iter([AIMessage(content=text) for text in responses]))


# --------------------------------------------------------------------------- #
# prompt 渲染（纯函数）
# --------------------------------------------------------------------------- #


def test_prompt_includes_description_and_criteria():
    prompt = render_task_prompt(_task(), ())
    assert "查询当前时间" in prompt
    assert "给出 ISO 8601 时间" in prompt


def test_prompt_includes_dependency_outputs():
    """没有这一段，depends_on 就只是画在面板上的装饰。"""
    deps = (TaskResult(task_id="t0", status="done", output="现在是 2026-09-28T18:04:13"),)
    prompt = render_task_prompt(_task(depends_on=["t0"]), deps)
    assert "2026-09-28T18:04:13" in prompt
    assert "t0" in prompt


def test_prompt_omits_dependency_section_when_no_deps():
    assert "上游任务结果" not in render_task_prompt(_task(), ())


def test_prompt_survives_dependency_without_output():
    deps = (TaskResult(task_id="t0", status="failed", error="boom"),)
    assert "boom" in render_task_prompt(_task(depends_on=["t0"]), deps)


# --------------------------------------------------------------------------- #
# final_text（纯函数）
# --------------------------------------------------------------------------- #


def test_final_text_returns_last_ai_message():
    state = {"messages": [HumanMessage("hi"), AIMessage("第一段"), AIMessage("最终答案")]}
    assert final_text(state) == "最终答案"


def test_final_text_skips_blank_and_tool_messages():
    state = {"messages": [AIMessage("有内容"), AIMessage("")]}
    assert final_text(state) == "有内容"


def test_final_text_handles_content_blocks():
    state = {"messages": [AIMessage(content=[{"type": "text", "text": "分块答案"}])]}
    assert final_text(state) == "分块答案"


@pytest.mark.parametrize("state", [None, {}, {"messages": []}, {"messages": [HumanMessage("只有用户话")]}])
def test_final_text_returns_none_when_nothing_visible(state):
    assert final_text(state) is None


# --------------------------------------------------------------------------- #
# 执行单元（异步生成器）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_emits_messages_events_with_json_safe_chunk():
    executor = make_react_executor(_model("你好", "世界"))

    events = await collect(executor(_invocation()))

    messages = [e for e in events if e.type == "messages"]
    assert messages, "至少要有一个 token 事件"
    json.dumps(messages[0].chunk)  # 必须是可 JSON 序列化的 dict，不是 LangChain 对象
    assert isinstance(messages[0].chunk, dict)


@pytest.mark.asyncio
async def test_terminal_event_carries_final_ai_text():
    executor = make_react_executor(_model("最终答案"))

    events = await collect(executor(_invocation()))

    terminal = [e for e in events if e.type == "task_end"]
    assert len(terminal) == 1
    assert terminal[0].task_id == "t1"
    assert terminal[0].status == "done"
    assert terminal[0].output == "最终答案"
    assert events[-1].type == "task_end", "终态事件必须是最后一个"


@pytest.mark.asyncio
async def test_model_error_is_absorbed_by_llm_middleware_not_by_us():
    """模型异常由 LLMErrorHandlingMiddleware 兜成兜底文案，任务仍然算 done。

    这是刻意记录的行为链：执行单元的 ``except Exception`` **不该**是模型错误的第一道
    防线（中间件已经拦了）。想当然地写「模型报错 → 任务失败」会把这条链写错。
    """

    class Exploding(ToolCapableFake):
        def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
            raise RuntimeError("model exploded")

    # 必须带工具：没有工具时 bind_tools 根本不会被调用，这个测试会静默变成「正常路径」。
    executor = make_react_executor(Exploding(messages=iter([AIMessage("never")])), [echo])

    events = await collect(executor(_invocation()))

    terminal = [e for e in events if e.type == "task_end"]
    assert len(terminal) == 1
    assert terminal[0].status == "done"
    assert terminal[0].output is not None
    assert "model exploded" in terminal[0].output


@pytest.mark.asyncio
async def test_graph_level_failure_becomes_failed_terminal_event():
    """非模型调用处的图级失败（如 checkpoint 存储不可用）不该掀翻整个运行。"""

    class ExplodingSaver(InMemorySaver):
        async def aput(self, config, checkpoint, metadata, new_versions):  # noqa: ANN001
            raise RuntimeError("checkpoint store down")

    executor = make_react_executor(_model("答案"), checkpointer=ExplodingSaver())

    events = await collect(executor(_invocation()))

    terminal = [e for e in events if e.type == "task_end"]
    assert len(terminal) == 1
    assert terminal[0].status == "failed"
    assert "checkpoint store down" in terminal[0].error


@pytest.mark.asyncio
async def test_terminal_event_when_model_produces_no_text():
    """空白回复不算失败：任务照常 done，只是没有可见产物。"""
    executor = make_react_executor(_model("   "))

    events = await collect(executor(_invocation()))

    terminal = [e for e in events if e.type == "task_end"]
    assert len(terminal) == 1
    assert terminal[0].status == "done"
    assert terminal[0].output is None


@pytest.mark.asyncio
async def test_prompt_reaches_the_model_with_dependency_output():
    model = RecordingFake(messages=iter([AIMessage(content="好")]))
    executor = make_react_executor(model)
    deps = (TaskResult(task_id="t0", status="done", output="上游产物标记XYZ"),)

    await collect(executor(_invocation(deps=deps)))

    assert model.last_human is not None
    assert "上游产物标记XYZ" in model.last_human


@pytest.mark.asyncio
async def test_executor_writes_checkpoint_under_run_scoped_thread_id():
    """thread_id 必须带 run_id：两个浏览器标签页会撞 task id。"""
    saver = InMemorySaver()
    executor = make_react_executor(_model("答案"), checkpointer=saver)

    await collect(executor(_invocation(run_id="run-1")))
    await collect(executor(_invocation(run_id="run-2")))

    threads = set()
    async for tup in saver.alist(None):
        threads.add(tup.config["configurable"]["thread_id"])
    assert threads == {"run-1:t1", "run-2:t1"}


@pytest.mark.asyncio
async def test_langgraph_multi_mode_still_yields_nested_tuples():
    """钉住上游行为：`values` 是状态字典，`messages` 的载荷自身又是 (chunk, metadata)。

    这条不是测我们的代码，是测我们对 langgraph 的假设。上游改了形状这里就会红。
    """
    from evaharness.agents.react import make_react_agent

    graph = make_react_agent(_model("hi"))
    seen_modes = set()
    async for item in graph.astream({"messages": [HumanMessage("hi")]}, stream_mode=["messages", "values"]):
        assert isinstance(item, tuple) and len(item) == 2
        mode, payload = item
        seen_modes.add(mode)
        if mode == "messages":
            assert isinstance(payload, tuple) and len(payload) == 2
        else:
            assert isinstance(payload, dict)
    assert seen_modes == {"messages", "values"}
