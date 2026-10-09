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
    skills: Sequence[Any] = (),
    **summarization: Any,
) -> AgentFactory:
    return AgentFactory(
        fake_app_config(**summarization),
        list(tools),
        checkpointer=checkpointer,
        model_factory=model_factory or _answering("好的。"),
        skills=list(skills),
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


# --------------------------------------------------------------------------- #
# 只有这次运行才有的工具（T13 的 MCP 工具）
# --------------------------------------------------------------------------- #


@tool("biz")
def _biz() -> str:
    """一个业务工具。"""
    return "x"


@tool("mcp__fs__read_file")
def _mcp_read(path: str) -> str:
    """一个 MCP 工具（名字已经带好前缀，装配层给的名字就是这样）。"""
    return path


def test_mcp_tools_reach_both_the_main_graph_and_the_subagent(monkeypatch):
    """MCP 工具与业务工具一样，必须同时进**主图与子图**。

    它们现在是启动期装载进 ``self.tools`` 的（`app.gateway.mcp`），所以本该**自动**跟着走——
    这条盯的就是「别把它们特殊化掉」：只进主图**不会报错**，只会让子 agent 少一批工具，那正是
    ``present_files`` 当初踩的静默失效。
    """
    import app.gateway.agents as agents_module

    seen: dict[str, list[str]] = {}
    real_subagent = agents_module.make_subagent_graph
    real_lead = agents_module.make_lead_agent

    def spy_subagent(model, tools=None, **kwargs):  # noqa: ANN001, ANN003
        seen["subagent"] = [t.name for t in (tools or [])]
        return real_subagent(model, tools, **kwargs)

    def spy_lead(model, tools=None, **kwargs):  # noqa: ANN001, ANN003
        seen["lead"] = [t.name for t in (tools or [])]
        return real_lead(model, tools, **kwargs)

    monkeypatch.setattr(agents_module, "make_subagent_graph", spy_subagent)
    monkeypatch.setattr(agents_module, "make_lead_agent", spy_lead)

    _factory(tools=[_biz, _mcp_read]).lead()

    assert seen["subagent"] == ["biz", "mcp__fs__read_file"], "MCP 工具没进子图（静默失效）"
    assert seen["lead"] == ["biz", "mcp__fs__read_file", TASK_TOOL_NAME]


def test_the_subagent_tool_list_is_a_blacklist_not_a_whitelist():
    """`_subagent_tools` 是**黑名单**（只剔 `present_files`），所以 MCP 工具默认能进。

    这条是防「为了让某类工具进子图，顺手把它改成白名单」那种改法——一改就会把没列进去的工具
    全部静默丢掉。
    """
    from app.gateway.agents import _subagent_tools

    assert _subagent_tools([_biz, _mcp_read]) == [_biz, _mcp_read]


# --------------------------------------------------------------------------- #
# 技能清单进提示词（T14）
# --------------------------------------------------------------------------- #


def _skill():
    from evaharness.skills import Skill

    return Skill(name="writer", description="帮我写文档", location="/skills/writer/SKILL.md")


def _spy_system_prompts(monkeypatch) -> dict[str, str]:
    """把四个 `make_*` 换成记录 `system_prompt` 的替身。"""
    import app.gateway.agents as agents_module

    seen: dict[str, str] = {}

    def spy_subagent(model, tools=None, **kwargs):  # noqa: ANN001, ANN003
        seen["subagent"] = kwargs.get("system_prompt") or ""
        return real_subagent(model, tools, **kwargs)

    def spy_lead(model, tools=None, **kwargs):  # noqa: ANN001, ANN003
        seen["lead"] = kwargs.get("system_prompt") or ""
        return real_lead(model, tools, **kwargs)

    def spy_executor(model, tools=None, **kwargs):  # noqa: ANN001, ANN003
        seen["executor"] = kwargs.get("system_prompt") or ""
        return real_executor(model, tools, **kwargs)

    def spy_planner(model, **kwargs):  # noqa: ANN001, ANN003
        seen["planner"] = kwargs.get("system_prompt") or ""
        return real_planner(model, **kwargs)

    real_subagent = agents_module.make_subagent_graph
    real_lead = agents_module.make_lead_agent
    real_executor = agents_module.make_react_executor
    real_planner = agents_module.make_planner

    monkeypatch.setattr(agents_module, "make_subagent_graph", spy_subagent)
    monkeypatch.setattr(agents_module, "make_lead_agent", spy_lead)
    monkeypatch.setattr(agents_module, "make_react_executor", spy_executor)
    monkeypatch.setattr(agents_module, "make_planner", spy_planner)
    return seen


def test_the_skill_section_reaches_lead_subagent_and_executor(monkeypatch):
    """技能清单必须同时进**三条** consumer——漏一处就是静默失效（`present_files` 的同款）。

    这条盯的是「谁把某处改回 `system_prompt=BASE`」：那不会报错，只会让那张图看不见技能，而症状是
    「模型明明该会用那个技能，却像没听说过」——非常难查。
    """
    seen = _spy_system_prompts(monkeypatch)
    factory = _factory(tools=[_biz], skills=[_skill()])

    factory.lead()
    factory.plan_exec()

    for where in ("subagent", "lead", "executor"):
        assert "writer" in seen[where], f"{where} 那张图没拿到技能清单（静默失效）"
        assert "/skills/writer/SKILL.md" in seen[where], f"{where} 那张图没拿到技能的读取位置"


def test_the_planner_does_not_get_the_skill_section(monkeypatch):
    """planner 走结构化输出、不调工具，读不了技能——与 `extra_tools` 同口径，别顺手给它。"""
    seen = _spy_system_prompts(monkeypatch)

    _factory(tools=[_biz], skills=[_skill()]).plan_exec()

    assert "writer" not in seen["planner"]


def test_no_skills_leaves_every_prompt_byte_for_byte_alone(monkeypatch):
    """没有技能时提示词必须与从前**逐字节相同**。

    拼装代码顺手多加一个换行，就等于所有没写技能的部署都拿到一份新提示词——前缀缓存白废，而且
    `test_lead_agent` 里那两条断言 prompt 取值的测试会红。这条把「空就一字不差」钉住。
    """
    from app.prompts import EXECUTOR_PROMPT, SUBAGENT_PROMPT, SYSTEM_PROMPT

    seen = _spy_system_prompts(monkeypatch)
    factory = _factory(tools=[_biz])

    factory.lead()
    factory.plan_exec()

    assert seen["lead"] == SYSTEM_PROMPT
    assert seen["subagent"] == SUBAGENT_PROMPT
    assert seen["executor"] == EXECUTOR_PROMPT
