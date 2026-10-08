"""契约测试：建图工厂（T9）。

本片把「建图」从 lifespan 挪进请求路径，依据是一条**实测**结论：共享中间件实例是并发
污染的唯一来源（其余五个默认中间件无状态）。所以这里的核心断言是「两份构建之间没有任何
中间件实例是同一个」——它是那个洞被消除的**充分条件**。

同时钉住「重建不会失忆」：记忆在 checkpointer 里，不在图里。
"""

import itertools
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from plan_exec_fakes import CallRecordingFake, ToolCapableFake, collect, fake_app_config

from app.gateway.agents import AgentFactory
from evaharness.agents.lead_agent import TASK_TOOL_NAME, run_lead_agent
from evaharness.agents.options import RunOptions
from evaharness.agents.middlewares import build_default_middlewares

ModelFactory = Callable[[str | None], ToolCapableFake]


def _answering(
    *answers: str,
    names: list[str | None] | None = None,
    options: list[RunOptions | None] | None = None,
) -> ModelFactory:
    """假模型的构造接缝。

    ``names`` / ``options`` 是记录用的：前者记用了哪个 profile，后者记每次运行带的参数
    ——「每请求参数真的传到了模型构造」这条就靠它断言。
    """
    texts = list(answers) or ["好的。"]

    def build(name: str | None, run_options: RunOptions | None = None) -> ToolCapableFake:
        if names is not None:
            names.append(name)
        if options is not None:
            options.append(run_options)
        return ToolCapableFake(messages=itertools.cycle([AIMessage(content=text) for text in texts]))

    return build


def _factory(
    *,
    tools: Sequence[Any] = (),
    checkpointer: Any | None = None,
    model_factory: ModelFactory | None = None,
    **summarization: Any,
) -> AgentFactory:
    return AgentFactory(
        fake_app_config(**summarization),
        list(tools),
        checkpointer=checkpointer,
        model_factory=model_factory or _answering("好的。"),
    )


# --------------------------------------------------------------------------- #
# 每请求一份
# --------------------------------------------------------------------------- #


def test_lead_returns_a_compiled_graph():
    assert isinstance(_factory().lead(), CompiledStateGraph)


def test_each_lead_build_is_a_new_graph():
    """**这条断言在 T9 之前根本写不出来**——那时 app.state 上挂的就是图本身。"""
    factory = _factory()

    assert factory.lead() is not factory.lead()


def test_the_subagent_never_gets_present_files():
    """子 agent 的工具集里必须**没有** ``present_files``。

    子 agent 用 ``ainvoke`` 跑、没有 custom 流模式，它那张图的 ``runtime.stream_writer`` 是
    no-op——工具在里面 emit 的东西石沉大海，**不抛异常、不报错**，只是卡片永远不出现。
    这种静默失效只能在结构上防（见 ``agents._subagent_tools``）。
    """
    from langchain_core.tools import tool

    from app.gateway.agents import _subagent_tools
    from app.tools.present_files import present_files

    @tool("other")
    def other(text: str) -> str:
        """一个普通业务工具。"""
        return text

    assert _subagent_tools([present_files, other]) == [other]


def test_each_plan_exec_build_is_a_new_roles_bundle():
    """三件套**必须一起换**：执行单元在装配期把图闭包进了 ``execute``。"""
    factory = _factory()

    first, second = factory.plan_exec(), factory.plan_exec()

    assert first is not second
    assert first.planner is not second.planner
    assert first.executor is not second.executor
    assert first.replanner is not second.replanner


def test_each_build_asks_for_a_fresh_model():
    """模型也不能缓存——图的结构要能按请求变，模型是其中一半。"""
    names: list[str | None] = []
    factory = _factory(model_factory=_answering(names=names))

    factory.lead()
    factory.plan_exec()

    assert names == [None, None], "每次构建都该重新构造模型"


def test_two_builds_share_no_middleware_instance(monkeypatch):
    """两次构建的中间件**互不相同**——并发洞被消除的充分条件。

    不去 ``graph.nodes`` 里翻中间件：那会把测试绑死在 LangChain 的编译产物结构上，
    框架一升级就红。改成在 ``build_default_middlewares`` 的**调用点**记下每次返回的链，
    再比同一性——spy 挂在函数被查找的那个模块上，所以它顺带断言了「每次构建都会重新要
    一份链」：一旦有人给链加缓存，第二次构建就记不到东西了。
    """
    seen: list[list] = []
    real = build_default_middlewares

    def spy() -> list:
        chain = real()
        seen.append(chain)
        return chain

    monkeypatch.setattr("evaharness.agents.lead_agent.agent.build_default_middlewares", spy)

    factory = _factory()
    factory.lead()
    first_build = list(seen)
    seen.clear()
    factory.lead()
    second_build = list(seen)

    assert first_build and second_build, "每次构建都该重新构建一份中间件链"
    assert len(first_build) == len(second_build)
    # 成对比较而不是比 id 集合：避免对象被回收后 id 复用的假阴性。
    assert all(a is not b for a in first_build for b in second_build), "两次构建共享了中间件实例"


def test_business_tools_reach_the_subagent_builder():
    """业务工具必须真的进到子图构建器——防递归那道理检查是它的证词。"""

    @tool(TASK_TOOL_NAME)
    def dispatch(prompt: str) -> str:
        """假的任务工具。"""
        return "x"

    with pytest.raises(ValueError, match="递归"):
        _factory(tools=[dispatch]).lead()


def test_the_summary_model_is_built_only_when_summarization_is_on():
    """摘要模型是**第二个**模型实例，只在「开了压缩且配了单独 profile」时才该建。"""
    off: list[str | None] = []
    _factory(enabled=False, model="cheap", model_factory=_answering(names=off)).lead()
    assert off == [None], "关了压缩就不该白建一个摘要模型"

    on: list[str | None] = []
    _factory(enabled=True, model="cheap", model_factory=_answering(names=on)).lead()
    assert on == [None, "cheap"], "开了压缩要按 profile 名建第二个模型"


# --------------------------------------------------------------------------- #
# 每请求参数（ADR-0011 预留的那个位置）
# --------------------------------------------------------------------------- #


def test_run_options_reach_the_model_factory():
    """「每请求参数真的传到了模型构造」——这是本片新增的那条链路的**唯一**断言点。"""
    seen: list[RunOptions | None] = []
    factory = _factory(model_factory=_answering("好的。", options=seen))

    factory.lead(RunOptions(thinking="max"))

    assert seen == [RunOptions(thinking="max")]


def test_no_options_still_calls_the_factory_with_none():
    """不带参数时传的是 ``None`` 而不是「随便给个默认档」——老调用方的行为要一字不变。"""
    seen: list[RunOptions | None] = []
    factory = _factory(model_factory=_answering("好的。", options=seen))

    factory.lead()

    assert seen == [None]


def test_the_summary_model_does_not_inherit_the_run_options():
    """压缩是内部辅助调用，不该跟着用户这轮选的思考档位跑——否则每次压上下文都变成一次昂贵思考。"""
    seen: list[RunOptions | None] = []
    factory = _factory(
        enabled=True, model="cheap", model_factory=_answering("好的。", options=seen)
    )

    factory.lead(RunOptions(thinking="max"))

    assert seen == [RunOptions(thinking="max"), None], "主模型带参数、摘要模型不带"


def test_plan_exec_also_receives_the_run_options():
    seen: list[RunOptions | None] = []
    factory = _factory(model_factory=_answering("好的。", options=seen))

    factory.plan_exec(RunOptions(thinking="low"))

    assert seen == [RunOptions(thinking="low")]


# --------------------------------------------------------------------------- #
# 记忆不随重建丢失
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_memory_survives_a_rebuild():
    """记忆在 checkpointer 里，不在图里：两张**不同**的图接着同一个会话坐标。

    坐标不变、图的身份变了——第 2 轮仍看得到第 1 轮。这条把「每请求重建不会失忆」
    从实测结论钉成回归。
    """
    saver = InMemorySaver()
    recorder = CallRecordingFake(messages=iter([AIMessage(content="好的，张三。"), AIMessage(content="你叫张三。")]))
    factory = _factory(checkpointer=saver, model_factory=lambda _name, _options=None: recorder)

    await collect(run_lead_agent("我叫张三", agent=factory.lead(), thread_id="th-1"))
    await collect(run_lead_agent("我叫什么", agent=factory.lead(), thread_id="th-1"))

    second_turn = recorder.calls[-1]
    humans = [str(m.content) for m in second_turn if isinstance(m, HumanMessage)]
    replies = [str(m.content) for m in second_turn if isinstance(m, AIMessage)]

    assert "我叫张三" in humans, "第一轮的用户消息不见了"
    assert "好的，张三。" in replies, "第一轮的助手回复不见了"
