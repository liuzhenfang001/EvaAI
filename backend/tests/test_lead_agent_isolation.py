"""两个 run 之间不共享中间件账本（T9）。

中间件的追踪状态（循环检测的 ``_history``、兜底重试的 ``_retried``）存在**实例字段**里。
共享一份实例时两个并发 run 会互相踩——实测：各调两次同一个工具，账本合起来就到
``warn_threshold=3``，谁先走到下一次模型调用谁就收到一段 ``[LOOP DETECTED]``。

**这个测试证明的是「一旦共享被重新引入，交错会让它原形毕露」**；「独立」那一半由
``test_agent_factory.py`` 里的结构断言负责（两次构建的中间件实例互不相同）。两者互为
动机：那条说的是「怎么保证」，这条说的是「不保证会怎样」。

交错用**事件闸门**造，不用 sleep——本机亚 16ms 的 sleep 会提前触发，靠它造时序必然飘。
"""

import asyncio

import pytest
from fakes import StreamToolFake, collect
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from evaharness.agents.lead_agent import make_lead_agent
from evaharness.runtime import run_agent

_TOOL_NAME = "probe"


class _Barrier:
    """等够 ``parties`` 个到达者就放行，然后自动进入下一轮。

    用它把两个 run 的交错钉死：第 1 轮的两个工具调用都到齐才一起返回，第 2 轮同理。
    """

    def __init__(self, parties: int, rounds: int) -> None:
        self._parties = parties
        self._count = 0
        self._round = 0
        self._events = [asyncio.Event() for _ in range(rounds)]

    async def wait(self) -> None:
        index = self._round
        self._count += 1
        if self._count >= self._parties:
            self._count = 0
            self._round += 1
            self._events[index].set()
        await asyncio.wait_for(self._events[index].wait(), timeout=5)


def _gated_tool(barrier: _Barrier):
    @tool(_TOOL_NAME)
    async def probe(x: str) -> str:
        """等齐另一个 run 的同类调用再返回（测试用的闸门）。"""
        await barrier.wait()
        return f"probe:{x}"

    return probe


def _scripted_model() -> StreamToolFake:
    """两次同样的工具调用，再来一段文本收尾——三轮模型调用。

    参数两次完全一样是**有意的**：循环检测按「工具名 + 稳定参数」的 hash 计数，不一样就
    永远撞不上阈值。
    """
    call = {"name": _TOOL_NAME, "args": {"x": "1"}}
    return StreamToolFake(
        messages=iter(
            [
                AIMessage(content="", tool_calls=[{**call, "id": "c1"}]),
                AIMessage(content="", tool_calls=[{**call, "id": "c2"}]),
                AIMessage(content="探完了。"),
            ]
        )
    )


def _warned(model: StreamToolFake) -> bool:
    return any(getattr(message, "name", None) == "loop_warning" for call in model.calls for message in call)


@pytest.mark.asyncio
async def test_two_runs_on_the_same_coordinate_keep_separate_ledgers():
    """两个 run 命中同一个会话坐标时，谁都不该收到对方累出来的循环警告。

    不接 checkpointer：本测试不依赖记忆，顺带避开「同一个 thread 并发写 checkpoint」
    这个 T9 不解决的既有竞争。
    """
    barrier = _Barrier(parties=2, rounds=2)
    tools = [_gated_tool(barrier)]
    models = [_scripted_model(), _scripted_model()]
    agents = [make_lead_agent(model, tools) for model in models]

    await asyncio.gather(
        collect(run_agent("探一下", agent=agents[0], thread_id="th-1")),
        collect(run_agent("探一下", agent=agents[1], thread_id="th-1")),
    )

    assert not any(_warned(model) for model in models), "一个 run 收到了另一个 run 累出来的循环警告"
