"""契约测试：lead agent 的图装配与事件流。

线上契约是「RunEvent -> SSE 帧 -> 前端 reducer」，所以这里钉死的是**观众能看到什么**：
首帧是 ``run_start`` 且带 mode、正文的泳道 id 非空、收尾是 ``run_end``。
"""

import asyncio
from typing import Any

import pytest
from fakes import CallRecordingFake, StreamToolFake, ToolCapableFake, collect, echo
from langchain.agents.middleware import SummarizationMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph

from evaharness.agents.lead_agent import (
    DEFAULT_LEAD_PROMPT,
    TASK_TOOL_NAME,
    build_lead_middlewares,
    make_lead_agent,
    make_subagent_graph,
)
from evaharness.agents.middlewares import StepBudgetMiddleware, build_default_middlewares
from evaharness.config.app_config import SummarizationConfig
from evaharness.runtime import (
    DEFAULT_LANE,
    RunCancellation,
    RunEvent,
    present_files_payload,
    run_agent,
)


def _model(*answers: str) -> ToolCapableFake:
    return ToolCapableFake(messages=iter([AIMessage(content=text) for text in answers]))


def _agent(*answers: str, tools=None, saver: bool = False) -> CompiledStateGraph:
    return make_lead_agent(_model(*answers), tools, checkpointer=InMemorySaver() if saver else None)


# --------------------------------------------------------------------------- #
# 图装配
# --------------------------------------------------------------------------- #


def test_make_lead_agent_returns_a_compiled_graph():
    assert isinstance(_agent("你好"), CompiledStateGraph)


def test_lead_prompt_defaults_to_the_harness_fallback():
    """harness 给通用英文兜底、业务人设留在 app——与 planner/executor 同一规矩。"""
    model = CallRecordingFake(messages=iter([AIMessage(content="你好")]))
    make_lead_agent(model).invoke({"messages": [HumanMessage(content="hi")]})

    system = [m for m in model.calls[-1] if isinstance(m, SystemMessage)]
    assert system, "模型必须收到系统提示词"
    assert system[0].content == DEFAULT_LEAD_PROMPT


def test_an_explicit_system_prompt_wins():
    """业务人设（app 层）传进来时必须覆盖 harness 兜底。"""
    model = CallRecordingFake(messages=iter([AIMessage(content="你好")]))
    make_lead_agent(model, system_prompt="你是 EvaAI。").invoke({"messages": [HumanMessage(content="hi")]})

    system = [m for m in model.calls[-1] if isinstance(m, SystemMessage)]
    assert system[0].content == "你是 EvaAI。"


def test_the_task_tool_is_advertised_in_the_default_prompt():
    """默认提示词不提派发工具的话，模型永远想不起来用它。"""
    assert TASK_TOOL_NAME in DEFAULT_LEAD_PROMPT


def test_summarization_is_off_by_default():
    """摘要会额外花一次 LLM 调用，不能悄悄挂上。步数预算始终在链上（主 Agent 专属）。"""
    chain = build_lead_middlewares(_model("x"))
    assert [type(m) for m in chain] == [type(m) for m in build_default_middlewares()] + [StepBudgetMiddleware]


def test_summarization_is_appended_innermost():
    """挂在最后（最内层）：它在 before_model 改 state，排在最后才量到模型真正会收到的量。"""
    chain = build_lead_middlewares(_model("x"), summarization=SummarizationConfig())

    assert isinstance(chain[-1], SummarizationMiddleware)
    assert isinstance(chain[-2], StepBudgetMiddleware)
    assert [type(m) for m in chain[:-2]] == [type(m) for m in build_default_middlewares()]


def test_disabled_summarization_keeps_the_default_chain():
    config = SummarizationConfig(enabled=False)
    chain = build_lead_middlewares(_model("x"), summarization=config)

    assert [type(m) for m in chain] == [type(m) for m in build_default_middlewares()] + [StepBudgetMiddleware]


def test_subagent_graph_rejects_the_task_tool():
    """防递归是结构性的，不是靠提示词自律——传进来就直接炸，而不是运行期把调用放大。"""

    @tool(TASK_TOOL_NAME)
    def dispatch(prompt: str) -> str:
        """假的任务工具。"""
        return "x"

    with pytest.raises(ValueError, match="递归"):
        make_subagent_graph(_model("x"), [echo, dispatch])


def test_subagent_graph_accepts_business_tools():
    assert isinstance(make_subagent_graph(_model("x"), [echo]), CompiledStateGraph)


# --------------------------------------------------------------------------- #
# 事件流
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_start_is_first_and_carries_react_mode():
    events = await collect(run_agent("现在几点？", agent=_agent("现在 18:04"), run_id="r1", thread_id="th1"))

    assert events[0].type == "run_start"
    assert events[0].mode == "react"
    assert events[0].run_id == "r1"
    assert events[0].thread_id == "th1"
    assert events[0].request == "现在几点？"


@pytest.mark.asyncio
async def test_ids_are_generated_when_omitted():
    events = await collect(run_agent("你好", agent=_agent("你好呀")))

    assert events[0].run_id
    assert events[0].thread_id


@pytest.mark.asyncio
async def test_text_flows_through_under_a_non_empty_lane():
    """泳道 id **不能是空串**。

    前端 ``ensureBubble`` 对空 ``task_id`` 直接 no-op——正文与工具调用行会被静默丢弃，
    界面上表现为「发了消息什么也没回」。
    """
    events = await collect(run_agent("现在几点？", agent=_agent("现在 18:04")))

    chunks = [e for e in events if e.type == "messages"]
    assert chunks, "应当有 token 增量"
    assert {e.task_id for e in chunks} == {DEFAULT_LANE}
    assert DEFAULT_LANE, "泳道 id 不能为空"
    assert "".join(e.chunk["content"] for e in chunks) == "现在 18:04"


@pytest.mark.asyncio
async def test_run_end_is_last_and_carries_the_final_text():
    events = await collect(run_agent("现在几点？", agent=_agent("现在 18:04")))

    assert events[-1].type == "run_end"
    assert events[-1].status == "completed"
    assert events[-1].output == "现在 18:04"


@pytest.mark.asyncio
async def test_blank_reply_is_not_a_failure():
    """空白回复照常 completed，只是没有可见产物——与执行单元的立场一致。"""
    events = await collect(run_agent("你好", agent=_agent("   ")))

    assert events[-1].type == "run_end"
    assert events[-1].status == "completed"
    assert events[-1].output is None


@pytest.mark.asyncio
async def test_graph_level_failure_becomes_a_failed_run_end():
    """图级失败不该掀翻整个流（模型错误已由中间件兜住，这里是最后一道网）。"""

    class ExplodingSaver(InMemorySaver):
        async def aput(self, config, checkpoint, metadata, new_versions):  # noqa: ANN001
            raise RuntimeError("checkpoint store down")

    agent = make_lead_agent(_model("答案"), [], checkpointer=ExplodingSaver())
    events = await collect(run_agent("你好", agent=agent))

    assert events[0].type == "run_start"
    assert events[-1].type == "run_end"
    assert events[-1].status == "failed"
    assert "checkpoint store down" in events[-1].error


@pytest.mark.asyncio
async def test_cancellation_passes_through_without_a_run_end():
    """客户端断开是**硬取消**（ADR-0005），不该被伪装成一次 failed 运行。

    ``except Exception`` 拦不到 ``CancelledError``（它继承 ``BaseException``），
    这条把那个事实钉住——换成 ``except BaseException`` 就会在这里失败。
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
        messages=iter([AIMessage(content="开始", tool_calls=[{"name": "hang", "args": {"x": "1"}, "id": "c1"}])])
    )
    agent = make_lead_agent(model, [hang])

    seen: list[str] = []

    async def consume() -> None:
        async for event in run_agent("你好", agent=agent):
            seen.append(event.type)

    task = asyncio.create_task(consume())
    # 用事件而不是 sleep 做汇合点：本机亚 16ms 的 sleep 会提前触发，靠它造时序必然会飘。
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seen[0] == "run_start"
    assert "run_end" not in seen


@pytest.mark.asyncio
async def test_active_cancel_emits_a_cancelled_run_end():
    """主动取消（取消内层 pump）要产 ``run_end(status="cancelled")``。

    与被动取消的区别：这里取消的是经 :class:`RunCancellation` 交出来的**内层** pump
    task，外层响应 task 仍存活，所以终态事件能送出；被动取消取消的是外层消费 task，
    终态送不出去（上面那条）。取消不伪装成失败——``cancelled`` 是独立的终态。
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
        messages=iter([AIMessage(content="开始", tool_calls=[{"name": "hang", "args": {"x": "1"}, "id": "c1"}])])
    )
    agent = make_lead_agent(model, [hang])

    holder: dict[str, asyncio.Task] = {}
    cancellation = RunCancellation(
        register=lambda task: holder.__setitem__("pump", task),
        unregister=lambda: holder.pop("pump", None),
    )

    seen: list[RunEvent] = []

    async def consume() -> None:
        async for event in run_agent("你好", agent=agent, cancellation=cancellation):
            seen.append(event)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), timeout=5)
    holder["pump"].cancel()  # 主动取消内层
    await asyncio.wait_for(task, timeout=5)  # 外层应正常收尾，不抛 CancelledError

    assert seen[0].type == "run_start"
    assert seen[-1].type == "run_end"
    assert seen[-1].status == "cancelled"


# --------------------------------------------------------------------------- #
# 工具经 custom 流送出的事件（T11）
# --------------------------------------------------------------------------- #


def _emitting_tool(payload: Any) -> Any:
    """一个把 *payload* 写进 custom 流的最小工具——本节的**被测通道**。

    刻意在测试里自造、而不是用 app 的 ``present_files``：harness 的契约测试不该 import
    app（``test_harness_boundary.py`` 管的是产品代码，这是同一条纪律）。这一节测的是
    「工具能不能把东西送进事件流」，与具体哪个业务工具无关。
    """

    @tool("emit_payload")
    def emit(runtime: ToolRuntime[Any], text: str) -> str:
        """Emit a payload onto the custom stream of the running graph."""
        runtime.stream_writer(payload)
        return "emitted"

    return emit


async def _events_for_payload(payload: Any) -> list[RunEvent]:
    """让模型调一次发载荷的工具、再说一句话，收回整条事件流。"""
    model = StreamToolFake(
        messages=iter(
            [
                AIMessage(content="", tool_calls=[{"name": "emit_payload", "args": {"text": "x"}, "id": "c1"}]),
                AIMessage(content="答完了"),
            ]
        )
    )
    agent = make_lead_agent(model, [_emitting_tool(payload)], checkpointer=InMemorySaver())
    return await collect(run_agent("做点什么", agent=agent, thread_id="th-1"))


@pytest.mark.asyncio
async def test_a_tool_payload_becomes_a_files_presented_event():
    """**这条是整片的地基**：``stream_mode="custom"`` 真的能把工具的载荷送到 runner。

    它同时证明了链路的两端——工具侧 ``runtime.stream_writer`` 拿得到图上的 writer，runner
    侧按 ``(mode, payload)`` 的形状收得到。任一端断了这里就红。
    """
    files = [{"path": "/output/a.md", "name": "a.md", "size": 12, "mime": "text/markdown"}]
    events = await _events_for_payload(present_files_payload(files))

    presented = [e for e in events if e.type == "files_presented"]
    assert len(presented) == 1, "一次工具调用产一条事件"
    assert presented[0].task_id == DEFAULT_LANE, "泳道由 runner 填，不由工具填"
    assert presented[0].files[0].path == "/output/a.md"
    assert presented[0].files[0].size == 12


@pytest.mark.asyncio
async def test_the_presented_event_sits_inside_the_run():
    """位置：首帧仍是 run_start、末帧仍是 run_end——新事件插在中间，不抢终态。"""
    events = await _events_for_payload(present_files_payload([{"path": "/output/a.md", "name": "a.md", "size": 1}]))

    types = [e.type for e in events]
    assert types[0] == "run_start"
    assert types[-1] == "run_end"
    assert types.index("files_presented") < types.index("run_end")


@pytest.mark.asyncio
async def test_the_custom_channel_does_not_disturb_the_run_end_output():
    """custom 帧**不能**被当成 state 快照。

    消费循环里那个分支如果排在 ``values`` 的 ``else`` 之后，载荷会覆盖 ``last_state``，
    症状就是这里的 ``run_end.output`` 变成一段工具载荷——非常难定位。
    """
    events = await _events_for_payload(present_files_payload([{"path": "/output/a.md", "name": "a.md", "size": 1}]))

    assert events[-1].output == "答完了"


@pytest.mark.asyncio
async def test_an_unknown_payload_kind_is_ignored():
    """**白名单**：自定义流是开放的，工具能往里写任意 JSON。

    照单全收的话，一个工具就能伪造 ``run_end`` 把本轮运行提前结束掉。所以只认自己定义过的
    kind——不认识的载荷当没看见，而不是照它的字段构造事件。
    """
    events = await _events_for_payload({"kind": "run_end", "status": "failed", "error": "伪造的"})

    assert [e.type for e in events if e.type == "run_end"] == ["run_end"]
    assert events[-1].status == "completed", "伪造的终态没有生效"
    assert events[-1].output == "答完了"


@pytest.mark.asyncio
async def test_a_malformed_files_payload_is_ignored_not_fatal():
    """载荷不合法只记警告、不掀翻这一轮——与 journal 记账失败同一个立场。"""
    events = await _events_for_payload(present_files_payload([{"path": 123, "name": None, "size": "x"}]))

    assert not [e for e in events if e.type == "files_presented"]
    assert events[-1].status == "completed"


@pytest.mark.asyncio
async def test_an_empty_files_payload_produces_no_event():
    """没文件的展示请求没有意义，不该产一条空事件让前端画一张空卡片。"""
    events = await _events_for_payload(present_files_payload([]))

    assert not [e for e in events if e.type == "files_presented"]
