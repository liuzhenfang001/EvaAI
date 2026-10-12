"""子 agent 派发：``task`` 工具的契约。

子 agent 的存在理由是**上下文隔离**，所以这里钉死的核心只有两条：它看不到父对话；
它的中间步骤也不会流回父对话。
"""

from dataclasses import dataclass
from typing import Any

import pytest
from fakes import CallRecordingFake, StreamToolFake, collect, echo
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import var_child_runnable_config

from evaharness.agents.lead_agent import (
    TASK_TOOL_NAME,
    make_lead_agent,
    make_subagent_graph,
    make_task_tool,
)
from evaharness.agents.lead_agent.subagent import NO_OUTPUT_PLACEHOLDER, _run_isolated
from evaharness.checkpoint import subagent_thread_id
from evaharness.runtime import RunContext, run_agent


@dataclass
class Harness:
    lead: Any
    lead_model: StreamToolFake
    sub_model: CallRecordingFake
    sub_saver: InMemorySaver


def _dispatch(prompt: str, call_id: str = "c1") -> AIMessage:
    """主 Agent 发出的一次派发。"""
    return AIMessage(
        content="我派个人去查。",
        tool_calls=[{"name": TASK_TOOL_NAME, "args": {"prompt": prompt}, "id": call_id}],
    )


def _harness(sub_messages: list, lead_messages: list, *, sub_tools: list | None = None) -> Harness:
    sub_saver = InMemorySaver()
    sub_model = CallRecordingFake(messages=iter(sub_messages))
    subagent = make_subagent_graph(
        sub_model,
        [echo] if sub_tools is None else sub_tools,
        checkpointer=sub_saver,
    )
    lead_model = StreamToolFake(messages=iter(lead_messages))
    lead = make_lead_agent(lead_model, [make_task_tool(subagent)], checkpointer=InMemorySaver())
    return Harness(lead=lead, lead_model=lead_model, sub_model=sub_model, sub_saver=sub_saver)


def _humans(messages) -> list[str]:  # noqa: ANN001
    return [str(m.content) for m in messages if isinstance(m, HumanMessage)]


async def _threads(saver: InMemorySaver) -> set[str]:
    return {str(entry.config["configurable"]["thread_id"]) async for entry in saver.alist(None)}


async def _run(harness: Harness, text: str, *, run_id: str = "r1", thread_id: str = "th-1"):
    return await collect(run_agent(text, agent=harness.lead, run_id=run_id, thread_id=thread_id))


@pytest.mark.asyncio
async def test_task_dispatches_a_subagent_and_returns_its_conclusion():
    harness = _harness([AIMessage(content="结论：42")], [_dispatch("查一下答案"), AIMessage(content="答案是 42。")])

    events = await _run(harness, "答案是多少")

    assert events[-1].status == "completed"
    assert events[-1].output == "答案是 42。"


@pytest.mark.asyncio
async def test_the_subagent_only_receives_the_prompt():
    """它**看不到**父对话——父对话这一段是有历史的，正好用来证明隔离。"""
    harness = _harness(
        [AIMessage(content="结论：张三")],
        [AIMessage(content="好的，张三。"), _dispatch("查一下我叫什么"), AIMessage(content="你叫张三。")],
    )

    await _run(harness, "我叫张三")
    await _run(harness, "我叫什么")

    # 子 agent 收到的人话**只有**这一条 prompt（外层是 InputSanitizationMiddleware 的边界标记），
    # 父对话那两轮一个字都没跟过来。
    humans = _humans(harness.sub_model.calls[0])
    assert len(humans) == 1
    assert "查一下我叫什么" in humans[0]


@pytest.mark.asyncio
async def test_the_subagent_gets_a_run_scoped_coordinate_carrying_the_call_id():
    """坐标带 run 与 tool_call_id：跨轮不累积，并发派发也不撞。"""
    harness = _harness([AIMessage(content="结论")], [_dispatch("干活", call_id="call-xyz"), AIMessage(content="好了")])

    await _run(harness, "去做")

    assert await _threads(harness.sub_saver) == {subagent_thread_id("th-1", "r1", "call-xyz")}


@pytest.mark.asyncio
async def test_the_subagents_steps_stay_out_of_the_parent_conversation():
    """隔离的另一半：子 agent 的中间步骤（工具往返、长篇输出）不回流到主对话。"""
    harness = _harness(
        [
            AIMessage(content="先查一下", tool_calls=[{"name": "echo", "args": {"text": "x"}, "id": "s1"}]),
            AIMessage(content="结论：x"),
        ],
        [_dispatch("干活"), AIMessage(content="好了")],
    )

    await _run(harness, "去做")

    parent_second_call = harness.lead_model.calls[-1]
    contents = [str(m.content) for m in parent_second_call]
    assert not any("先查一下" in text for text in contents), "子 agent 的内部推理不该进主对话"
    assert any("结论：x" in text for text in contents), "结论必须回到主对话"

    subagent_call_ids = {
        call.get("id") for message in parent_second_call for call in (getattr(message, "tool_calls", None) or [])
    }
    assert "s1" not in subagent_call_ids, "子 agent 的工具调用不该混进主对话"


@pytest.mark.asyncio
async def test_the_subagent_can_use_business_tools():
    harness = _harness(
        [
            AIMessage(content="查", tool_calls=[{"name": "echo", "args": {"text": "x"}, "id": "s1"}]),
            AIMessage(content="结论：x"),
        ],
        [_dispatch("干活"), AIMessage(content="好了")],
    )

    await _run(harness, "去做")

    tool_results = [m for m in harness.sub_model.calls[-1] if isinstance(m, ToolMessage)]
    assert [str(m.content) for m in tool_results] == ["x"]


@pytest.mark.asyncio
async def test_a_subagent_that_says_nothing_returns_a_placeholder():
    """空串进了 tool message 后模型往往会重新派发一次同样的活，所以给一句话。"""
    harness = _harness([AIMessage(content="   ")], [_dispatch("干活"), AIMessage(content="好了")])

    await _run(harness, "去做")

    tool_results = [m for m in harness.lead_model.calls[-1] if isinstance(m, ToolMessage)]
    assert [str(m.content) for m in tool_results] == [NO_OUTPUT_PLACEHOLDER]


# --------------------------------------------------------------------------- #
# 隔离的实现细节：干净配置的进出
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_subagent_runs_with_a_clean_config():
    """子 agent 内部必须是干净的：继承宿主的 pregel 内部键就会「跑一步就停」。"""

    class Stub:
        async def ainvoke(self, payload, config, context):  # noqa: ANN001, ANN003
            assert var_child_runnable_config.get() is None, "宿主那套 pregel 键必须已经摘掉"
            assert config == {"configurable": {"thread_id": "th-1:r1:sub:c1"}}
            return {"messages": []}

    await _run_isolated(Stub(), {}, coordinate="th-1:r1:sub:c1", context=RunContext("th-1", "th-1:lead", "r1"))


@pytest.mark.asyncio
async def test_the_clean_config_does_not_leak_into_the_caller():
    """``set(None)`` 用完必须还原。

    泄漏出去的话，同一个任务里**后续**的图调用会跟着「失忆」——而且只在特定调用顺序下
    才发作。实测去掉 ``reset``，上面那几条子 agent 测试照样全绿，所以这条必须单独钉住。
    """

    class Stub:
        async def ainvoke(self, payload, config, context):  # noqa: ANN001, ANN003
            return {"messages": []}

    marker = {"configurable": {"thread_id": "sentinel"}}
    token = var_child_runnable_config.set(marker)
    try:
        await _run_isolated(Stub(), {}, coordinate="c", context=RunContext("s", "c", "r"))
        assert var_child_runnable_config.get() is marker
    finally:
        var_child_runnable_config.reset(token)
