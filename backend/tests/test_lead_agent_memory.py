"""lead agent 的多轮记忆：靠**会话级固定坐标**让 checkpointer 自动装载历史。

这是 T8 的立身之本。T8 之前 Plan-Exec 的执行单元每轮换一个坐标，于是每轮都开一个空
thread——界面上是一段连续对话，模型却一个字都记不住。这里把机制钉死。
"""

import asyncio

import pytest
from fakes import CallRecordingFake, StreamToolFake, collect
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph

from evaharness.agents.lead_agent import make_lead_agent
from evaharness.checkpoint import delete_session_checkpoints, lead_thread_id
from evaharness.runtime import run_agent


def _build(*answers: str, saver=None) -> tuple[CompiledStateGraph, CallRecordingFake]:
    recorder = CallRecordingFake(messages=iter([AIMessage(content=text) for text in answers]))
    return make_lead_agent(recorder, [], checkpointer=saver or InMemorySaver()), recorder


def _humans(messages) -> list[str]:  # noqa: ANN001
    return [str(m.content) for m in messages if isinstance(m, HumanMessage)]


def _ai_texts(messages) -> list[str]:  # noqa: ANN001
    return [str(m.content) for m in messages if isinstance(m, AIMessage)]


async def _threads(saver: InMemorySaver) -> set[str]:
    return {str(entry.config["configurable"]["thread_id"]) async for entry in saver.alist(None)}


async def _say(agent: CompiledStateGraph, text: str, thread_id: str) -> None:
    await collect(run_agent(text, agent=agent, thread_id=thread_id))


@pytest.mark.asyncio
async def test_the_second_turn_sees_the_first_turn():
    """同一个会话里，第二轮必须能看见第一轮——这就是「记忆」。"""
    agent, recorder = _build("好的，张三。", "你叫张三。")

    await _say(agent, "我叫张三", "th-1")
    await _say(agent, "我叫什么", "th-1")

    second_call = recorder.calls[-1]
    assert _humans(second_call)[0] == "我叫张三"
    assert "我叫什么" in _humans(second_call)[1]
    assert "好的，张三。" in _ai_texts(second_call), "上一轮的助手回复也必须带上"


@pytest.mark.asyncio
async def test_a_new_session_starts_empty():
    """换一个会话就该是一张白纸。"""
    agent, recorder = _build("好的，张三。", "我不知道你叫什么。")

    await _say(agent, "我叫张三", "th-1")
    await _say(agent, "我叫什么", "th-2")

    # 只有这一轮的用户消息（外层是 InputSanitizationMiddleware 包的边界标记）。
    humans = _humans(recorder.calls[-1])
    assert len(humans) == 1
    assert "我叫什么" in humans[0]


@pytest.mark.asyncio
async def test_the_checkpoint_lands_on_the_session_scoped_coordinate():
    """坐标必须是 ``{会话}:lead`` 且**跨轮不变**——这正是记忆的来源。

    带上 run 就退回 T8 之前那个「每轮空 thread」的状态，而症状只是「模型突然变傻」，
    从行为上很难反推到坐标。所以这里直接查库。
    """
    saver = InMemorySaver()
    agent, _ = _build("好的。", "知道了。", saver=saver)

    await _say(agent, "第一轮", "th-1")
    await _say(agent, "第二轮", "th-1")

    assert await _threads(saver) == {lead_thread_id("th-1")}


@pytest.mark.asyncio
async def test_deleting_the_session_takes_the_memory_with_it():
    """删会话要连记忆一起删——留着的话，已经删掉的对话还在被模型「记得」。"""
    saver = InMemorySaver()
    agent, _ = _build("好的。", saver=saver)
    await _say(agent, "我叫张三", "th-1")

    deleted = await delete_session_checkpoints(saver, thread_id="th-1")

    assert deleted == 1
    assert await _threads(saver) == set()


@pytest.mark.asyncio
async def test_a_turn_interrupted_mid_tool_still_leaves_the_request_in_memory():
    """被中断的运行（没跑到终态）也得让下一轮看得到用户说了什么。

    resume 收口（T5）：不做断点续跑，但「没得到回复的消息」必须留在会话记忆里，
    下一轮模型要能看见它并补答。中断发生在工具执行中——正是最「半截」的状态：用户
    已经说了，AI 还没答。
    """
    entered = asyncio.Event()
    blocker = asyncio.Event()

    @tool
    async def hang(x: str) -> str:
        """永不返回的工具。"""
        entered.set()
        await blocker.wait()
        return "never"

    model = StreamToolFake(
        messages=iter(
            [
                AIMessage(content="开始", tool_calls=[{"name": "hang", "args": {"x": "1"}, "id": "c1"}]),
                AIMessage(content="补答：你刚才问的是「我的问题」"),
            ]
        )
    )
    agent = make_lead_agent(model, [hang], checkpointer=InMemorySaver())

    async def first() -> None:
        async for _ in run_agent("我的问题", agent=agent, thread_id="th-1"):
            pass

    task = asyncio.create_task(first())
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()  # 被动取消：没产 run_end，这一轮「半截」悬着
    with pytest.raises(asyncio.CancelledError):
        await task

    # 第二轮同一会话：模型应当看得到第一轮的用户消息并补答
    await _say(agent, "接着答", "th-1")

    assert "我的问题" in _humans(model.calls[-1])


# --------------------------------------------------------------------------- #
# 会话摘要压缩
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_summarization_shrinks_what_the_next_turn_sees():
    """摘要压缩**真的**会改模型看到的历史。

    「中间件挂上了」和「它起作用」是两回事：``build_lead_middlewares`` 里多一个对象
    用类型断言就能测，但阈值、``keep`` 与模型的接线错了它照样绿。所以这里直接量
    「第 N 轮模型收到几条消息」。
    """
    from evaharness.agents.lead_agent import build_lead_middlewares
    from evaharness.config.app_config import SummarizationConfig

    recorder = CallRecordingFake(messages=iter([AIMessage(content=f"回答{i}") for i in range(40)]))
    agent = make_lead_agent(
        recorder,
        [],
        middleware=build_lead_middlewares(
            recorder,
            summarization=SummarizationConfig(trigger_tokens=20, keep_messages=2),
        ),
        checkpointer=InMemorySaver(),
    )

    for turn in range(5):
        await _say(agent, f"第{turn}轮：这是一句用来把上下文撑长一点的话", "th-1")

    # 不压缩的话第 5 轮会看到 1 + 4*2 = 9 条；压缩后明显少于此。
    assert len(recorder.calls[-1]) < 9, "上下文没有被压缩"


@pytest.mark.asyncio
async def test_summarization_is_off_by_default():
    """默认不挂：压缩要额外花一次 LLM 调用，不能悄悄发生。"""
    from evaharness.agents.lead_agent import build_lead_middlewares

    recorder = CallRecordingFake(messages=iter([AIMessage(content=f"回答{i}") for i in range(40)]))
    agent = make_lead_agent(recorder, [], middleware=build_lead_middlewares(recorder), checkpointer=InMemorySaver())

    for turn in range(5):
        await _say(agent, f"第{turn}轮", "th-1")

    # 第 5 次调用看到的是：1 条系统 + 前 4 轮的 8 条 + 本轮的 1 条用户消息 = 10。
    assert len(recorder.calls[-1]) == 10, "历史应当原样保留，一条都不该少"
