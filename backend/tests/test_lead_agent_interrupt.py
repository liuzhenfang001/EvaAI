"""图**挂起**时 runner 怎么收场。

`interrupt()` 不是异常——图安静地停下，`__interrupt__` 落在 values 快照里（T13 实测：进程内
MCP server 用 guard pattern 要输入时，`stream_mode="values"` 最后一条正是
``{'out': None, '__interrupt__': (Interrupt(value={'type': 'mcp_elicitation', ...}),)}``）。

不处理它的话，那一行 `else: last_state = data` 会把它整个塞进 `last_state`，这一轮以
``completed`` 收场，模型永远等不到工具结果，而用户只看到「莫名其妙没下文」——正是本项目最
反感的那种静默失效。所以这里把它**翻译成一条可见的失败**。

测试用**真的**会挂起的图（而不是造一个假 payload），这样断言不会与 langgraph 的实际行为漂移。
"""

from __future__ import annotations

from typing import Any, TypedDict

import pytest
from fakes import collect
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from evaharness.agents.lead_agent import LeadContext, run_lead_agent
from evaharness.checkpoint import lead_thread_id

MCP_PAYLOAD: dict[str, Any] = {
    "type": "mcp_elicitation",
    "tool_name": "ask_name",
    "requests": [
        {"key": "q1", "message": "你叫什么名字？", "mode": "form", "requested_schema": {}},
    ],
}


class _State(TypedDict, total=False):
    messages: list


def _graph_that_interrupts(payload: Any):
    """一个编译好的小图：唯一的节点一进去就挂起。"""

    async def node(_state):  # noqa: ANN001, ANN202
        interrupt(payload)
        return {}  # pragma: no cover —— 挂起之后不会走到这里

    builder = StateGraph(_State, context_schema=LeadContext)
    builder.add_node("n", node)
    builder.add_edge(START, "n")
    builder.add_edge("n", END)
    return builder.compile(checkpointer=InMemorySaver())


@pytest.mark.asyncio
async def test_an_unanswered_mcp_elicitation_ends_the_run_visibly():
    """挂起要以 **failed** 收场，而且错误里要看得出「是谁在问什么」。

    前端拿到 ``status="failed"`` 会追加一句「运行失败：{error}」（`applyRunEnd`），所以这条
    链路是通的——错误文案就是用户能看到的全部内容。
    """
    events = await collect(
        run_lead_agent("你好", agent=_graph_that_interrupts(MCP_PAYLOAD), thread_id="th1")
    )

    end = events[-1]
    assert end.type == "run_end"
    assert end.status == "failed", "挂起被当成正常完成了——用户会看到一段没有下文的对话"
    assert end.error is not None
    assert "ask_name" in end.error, "没说清是哪个工具在问"
    assert "你叫什么名字？" in end.error, "没把问题本身带给用户"


@pytest.mark.asyncio
async def test_a_payload_that_is_not_elicitation_still_ends_visibly():
    """不认识的 interrupt 也不能被静默吞掉——通用那句同样要以 failed 收场。"""
    events = await collect(
        run_lead_agent("你好", agent=_graph_that_interrupts({"type": "something_else"}), thread_id="th2")
    )

    end = events[-1]
    assert end.status == "failed"
    assert end.error is not None
    assert "interrupt" in end.error


@pytest.mark.asyncio
async def test_a_run_without_any_interrupt_still_completes():
    """反向护栏：没有挂起的运行**不受影响**，仍然是 completed。

    否则上面两条测试只要「把 completed 全改成 failed」就能骗过去。
    """

    async def node(_state):  # noqa: ANN001, ANN202
        return {}

    builder = StateGraph(_State, context_schema=LeadContext)
    builder.add_node("n", node)
    builder.add_edge(START, "n")
    builder.add_edge("n", END)
    graph = builder.compile(checkpointer=InMemorySaver())

    events = await collect(run_lead_agent("你好", agent=graph, thread_id="th3"))

    assert events[-1].status == "completed"


@pytest.mark.asyncio
async def test_resuming_a_pending_interrupt_lets_the_run_continue():
    """挂起**不是终点**：带答案重入同一个 checkpoint 坐标，图会从停下的地方接着跑。

    这条钉住的是「我们的架构支持恢复」这件事——**状态在 checkpointer 里，不在 MCP 连接里**。
    所以恢复可以发生在**另一个请求**里（也就意味着另一条 MCP 连接、stdio 下另一个子进程）。
    这一点是实测的：真子进程起来、挂起、连接关掉（进程随之结束），换一个全新子进程带答案进去，
    工具照样跑完并返回结果。前提见下：server 得是「凭 input_responses 重算」的那种（或把状态
    编码进 `request_state`），把活儿存在自己进程内存里的 server 换进程就接不上了。

    坐标必须**一模一样**：`run_lead_agent` 用的是 ``lead_thread_id(会话)`` = ``{会话}:lead``，
    恢复时也得用它，否则 checkpointer 里找不到那段挂起的状态。
    """
    saver = InMemorySaver()
    answers: list[Any] = []

    async def node(_state):  # noqa: ANN001, ANN202
        answers.append(interrupt(MCP_PAYLOAD))
        return {}

    builder = StateGraph(_State, context_schema=LeadContext)
    builder.add_node("n", node)
    builder.add_edge(START, "n")
    builder.add_edge("n", END)
    graph = builder.compile(checkpointer=saver)

    # 第一段：走我们的 runner。挂起被报成 failed，而 node 停在 interrupt() 上。
    events = await collect(run_lead_agent("你好", agent=graph, thread_id="th-resume"))
    assert events[-1].status == "failed"
    assert answers == [], "还没人回答，node 不该往下走"

    # 第二段：带答案重入**同一个坐标**，图接着跑。
    resume = {"responses": {"q1": {"action": "accept", "content": {"name": "小明"}}}}
    await graph.ainvoke(
        Command(resume=resume),
        config={"configurable": {"thread_id": lead_thread_id("th-resume")}},
        context=LeadContext(
            session_id="th-resume", thread_id=lead_thread_id("th-resume"), run_id="r2"
        ),
    )

    assert answers == [resume], "resume 的值应当**原样**成为 interrupt() 的返回值"
