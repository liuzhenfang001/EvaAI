"""记忆中间件的契约：**thread 开头写一次、轮末采集一次**。

T20 的契约是「注入只活在这一次模型调用里」，测试钉的正是它的反面。T21 反过来了：

- 注入要**进 state**（否则每个请求、每个 ReAct 步骤都得重放一遍）；
- 但只进一次（``<memory>`` 在会话里只出现一条，不随轮次堆积）；
- 采集是旁路（失败不影响这一轮，消息也不进 journal）。
"""

from __future__ import annotations

import pytest
from fakes import CallRecordingFake, collect
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from evaharness.agents.lead_agent import build_lead_middlewares, make_lead_agent
from evaharness.agents.middlewares import (
    MemoryCaptureMiddleware,
    MemoryMiddleware,
    build_default_middlewares,
)
from evaharness.checkpoint import lead_thread_id
from evaharness.memory import MEMORY_TAG
from evaharness.runtime import RunEvent, run_agent
from evaharness.runtime.event.translate import translate

BLOCK = "<memory>\n用户偏好中文回答。\n</memory>"


def _agent(*answers: str, renderer=None, capture=None, saver=None):
    recorder = CallRecordingFake(messages=iter([AIMessage(content=text) for text in answers]))
    chain = build_lead_middlewares(recorder, memory=renderer, memory_capture=capture)
    return make_lead_agent(recorder, [], middleware=chain, checkpointer=saver or InMemorySaver()), recorder


async def _say(agent, text: str, thread_id: str = "th-1") -> list[RunEvent]:
    return await collect(run_agent(text, agent=agent, thread_id=thread_id))


async def _state_messages(agent, thread_id: str = "th-1") -> list:
    snapshot = await agent.aget_state({"configurable": {"thread_id": lead_thread_id(thread_id)}})
    return list(snapshot.values["messages"])


def _memory_messages(messages) -> list:
    return [
        message
        for message in messages
        if getattr(message, "name", None) == MEMORY_TAG
        and (getattr(message, "additional_kwargs", None) or {}).get("hide_from_ui")
    ]


# --------------------------------------------------------------------------- #
# 链的组成
# --------------------------------------------------------------------------- #


def test_the_default_chain_carries_neither_memory_middleware():
    """默认关——没开这个功能的部署不该多中间件（`test_lead_agent.py` 也依赖这条）。"""
    defaults = build_default_middlewares()
    assert not any(isinstance(item, MemoryMiddleware) for item in defaults)
    assert not any(isinstance(item, MemoryCaptureMiddleware) for item in defaults)


def test_the_middleware_is_appended_only_when_a_renderer_is_given():
    recorder = CallRecordingFake(messages=iter([AIMessage(content="x")]))
    without = build_lead_middlewares(recorder)
    with_memory = build_lead_middlewares(recorder, memory=lambda: BLOCK)
    with_capture = build_lead_middlewares(recorder, memory_capture=lambda _sid, _msgs: None)

    assert not any(isinstance(item, MemoryMiddleware) for item in without)
    assert any(isinstance(item, MemoryMiddleware) for item in with_memory)
    assert any(isinstance(item, MemoryCaptureMiddleware) for item in with_capture)


# --------------------------------------------------------------------------- #
# 注入：进 state，但只一次
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_memory_block_reaches_the_model():
    agent, recorder = _agent("好", renderer=lambda: BLOCK)

    await _say(agent, "你好")

    assert any(BLOCK in str(message.content) for message in recorder.calls[-1])


@pytest.mark.asyncio
async def test_the_injected_message_is_marked_hidden_and_named():
    """``hide_from_ui`` 是别的中间件认它的依据（输入清洗不当真用户消息、兜底回复不算工具回合）。"""
    agent, _ = _agent("好", renderer=lambda: BLOCK)

    await _say(agent, "你好")

    injected = _memory_messages(await _state_messages(agent))
    assert len(injected) == 1
    assert isinstance(injected[0], HumanMessage)
    assert injected[0].name == MEMORY_TAG
    assert injected[0].additional_kwargs["hide_from_ui"] is True
    assert injected[0].id == "memory-injection:th-1"


@pytest.mark.asyncio
async def test_the_block_is_written_once_per_thread():
    """第二轮不会再多一条；模型仍从历史里看得到它。"""
    agent, recorder = _agent("第一次", "第二次", renderer=lambda: BLOCK)

    await _say(agent, "甲")
    await _say(agent, "乙")

    messages = await _state_messages(agent)
    assert len(_memory_messages(messages)) == 1
    assert sum(1 for message in recorder.calls[-1] if str(message.content) == BLOCK) == 1


@pytest.mark.asyncio
async def test_a_summarized_away_marker_is_reinjected():
    """摘要压缩把记忆消息删掉后补注一次——这是「thread 内一次」唯一的例外。

    严格一次与「压缩后记忆不丢」只能二选一；这张测试把选择钉住，也确保补注不会变成堆积。
    """
    from langchain_core.messages import RemoveMessage

    agent, _ = _agent("第一次", "第二次", renderer=lambda: BLOCK)
    await _say(agent, "甲")

    config = {"configurable": {"thread_id": lead_thread_id("th-1")}}
    snapshot = await agent.aget_state(config)
    injected = _memory_messages(list(snapshot.values["messages"]))[0]
    await agent.aupdate_state(config, {"messages": [RemoveMessage(id=injected.id)]})

    await _say(agent, "乙")

    assert len(_memory_messages(await _state_messages(agent))) == 1


@pytest.mark.asyncio
async def test_an_empty_first_turn_does_not_burn_the_once_per_thread_budget():
    """空库首轮不注入；等记忆真正有内容时，第一次遇到非空快照的那一轮注入。

    这条钉的是「首轮判定用 marker 而不是『有没有 AIMessage』」——用后者的话，空库起步的
    会话会永远看不到后来积累的记忆。
    """
    blocks = iter(["", BLOCK])
    agent, _ = _agent("第一次", "第二次", renderer=lambda: next(blocks, BLOCK))

    await _say(agent, "甲")
    assert _memory_messages(await _state_messages(agent)) == []

    await _say(agent, "乙")
    assert len(_memory_messages(await _state_messages(agent))) == 1


# --------------------------------------------------------------------------- #
# 空与失败
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_empty_render_leaves_the_state_untouched():
    plain, _ = _agent("好")
    empty, _ = _agent("好", renderer=lambda: "")

    await _say(plain, "你好")
    await _say(empty, "你好")

    assert [str(m.content) for m in await _state_messages(plain)] == [
        str(m.content) for m in await _state_messages(empty)
    ]


@pytest.mark.asyncio
async def test_a_failing_render_does_not_break_the_turn():
    def boom() -> str:
        raise RuntimeError("库挂了")

    agent, _ = _agent("好", renderer=boom)

    events = await _say(agent, "你好")

    assert events
    assert _memory_messages(await _state_messages(agent)) == []


# --------------------------------------------------------------------------- #
# 采集
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_capture_runs_at_the_end_of_the_turn_with_the_session_id():
    captured: list[tuple[str, list[str]]] = []

    def capture(session: str, messages) -> None:
        captured.append((session, [str(message.content) for message in messages]))

    agent, _ = _agent("好", capture=capture)

    await _say(agent, "你好")

    assert captured
    assert captured[-1][0] == "th-1"
    assert "你好" in captured[-1][1]
    assert "好" in captured[-1][1]


@pytest.mark.asyncio
async def test_a_failing_capture_does_not_break_the_turn():
    def boom(_session, _messages) -> None:
        raise RuntimeError("队列挂了")

    agent, _ = _agent("好", capture=boom)

    events = await _say(agent, "你好")

    assert events
    assert any(getattr(event, "status", None) == "completed" for event in events)


# --------------------------------------------------------------------------- #
# 不进 journal
# --------------------------------------------------------------------------- #


def test_a_hidden_message_never_becomes_a_stream_event():
    """即使上游哪天把 state 里的消息也塞进 ``messages`` 流，这层也会把它挡掉。

    当前不会发生（中间件节点 ``trace=False``），但前端历史完全建立在 journal 上，值得有一道
    「按标记语义」的保险。
    """
    event = translate(
        "messages",
        (HumanMessage(content=BLOCK, additional_kwargs={"hide_from_ui": True}), {}),
        lane="lead",
    )
    assert event is None
