"""契约测试：planner / replanner 的装配。

这里最重要的是三条**绑定**测试。``ChatOpenAI.with_structured_output`` 的默认
``method`` 是 ``json_schema``，而自动降级白名单只认 ``gpt-3*`` / ``gpt-4*`` /
``gpt-4``——``deepseek-chat`` 不在其中。默认路径会让 DeepSeek 端点直接 HTTP 400
（``response_format`` 类型不支持）。这三条测试就是那个坑的回归防线。
"""

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from plan_exec_fakes import StructuredFake

from evaharness.agents.plan_exec.planner import DEFAULT_PLANNER_PROMPT, make_planner
from evaharness.agents.plan_exec.replanner import make_replanner
from evaharness.agents.plan_exec.schemas import Plan, PlanTask, ReplanDecision, TaskResult


def _chat_openai() -> ChatOpenAI:
    """只构造、不发请求（同 test_model_factory.py 的做法）。"""
    return ChatOpenAI(
        model="deepseek-chat",
        api_key="sk-dummy",
        base_url="https://api.deepseek.com/v1",
    )


def _bound_tools(runnable) -> list[dict]:
    """从 with_structured_output 产出的 RunnableSequence 里取出绑定的工具定义。"""
    return runnable.first.kwargs["tools"]


# --------------------------------------------------------------------------- #
# 绑定：DeepSeek 兼容性防线
# --------------------------------------------------------------------------- #


def test_make_planner_binds_function_calling_by_default():
    planner = make_planner(_chat_openai())

    tools = _bound_tools(planner.runnable)

    assert [t["function"]["name"] for t in tools] == ["Plan"]
    assert "strict" not in tools[0]["function"], "DeepSeek 不支持 strict"
    assert "response_format" not in planner.runnable.first.kwargs, "默认的 json_schema 路径会让 DeepSeek 报 400"


def test_make_replanner_binds_function_calling_by_default():
    replanner = make_replanner(_chat_openai())

    tools = _bound_tools(replanner.runnable)

    assert [t["function"]["name"] for t in tools] == ["ReplanDecision"]
    assert "strict" not in tools[0]["function"]


def test_explicit_method_is_honored():
    """OpenAI 官方端点想用 json_schema 时，开关要能拨过去。"""
    planner = make_planner(_chat_openai(), method="json_schema")

    assert "response_format" in planner.runnable.first.kwargs
    assert planner.method == "json_schema"


def test_method_defaults_are_recorded():
    assert make_planner(_chat_openai()).method == "function_calling"
    assert make_replanner(_chat_openai()).method == "function_calling"


# --------------------------------------------------------------------------- #
# 调用：消息组装与结果透传
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_planner_sends_system_and_user_messages_and_returns_plan():
    expected = Plan(tasks=[PlanTask(id="t1", description="查时间")])
    model = StructuredFake(result=expected)
    planner = make_planner(model, system_prompt="你是规划器")

    plan = await planner("现在几点？")

    assert plan is expected
    assert model.structured_method == "function_calling"
    assert isinstance(model.seen_messages[0], SystemMessage)
    assert model.seen_messages[0].content == "你是规划器"
    assert isinstance(model.seen_messages[1], HumanMessage)
    assert model.seen_messages[1].content == "现在几点？"


@pytest.mark.asyncio
async def test_planner_falls_back_to_default_prompt():
    """``None`` 的语义是「用 harness 默认提示词」，不是「不要系统消息」。"""
    model = StructuredFake(result=Plan(tasks=[PlanTask(id="t1", description="d")]))
    planner = make_planner(model, system_prompt=None)

    await planner("随便")

    assert [type(m).__name__ for m in model.seen_messages] == ["SystemMessage", "HumanMessage"]
    assert model.seen_messages[0].content == DEFAULT_PLANNER_PROMPT


@pytest.mark.asyncio
async def test_empty_system_prompt_sends_only_the_user_message():
    model = StructuredFake(result=Plan(tasks=[PlanTask(id="t1", description="d")]))
    planner = make_planner(model, system_prompt="")

    await planner("随便")

    assert [type(m).__name__ for m in model.seen_messages] == ["HumanMessage"]


@pytest.mark.asyncio
async def test_replanner_receives_request_plan_and_results():
    model = StructuredFake(result=ReplanDecision(should_continue=False, reason="够了"))
    replanner = make_replanner(model, system_prompt="你是复核者")
    plan = Plan(tasks=[PlanTask(id="t1", description="查时间")])
    results = [TaskResult(task_id="t1", status="done", output="现在是 18:04")]

    decision = await replanner("现在几点？", plan, results)

    assert decision.should_continue is False
    human = next(m for m in model.seen_messages if isinstance(m, HumanMessage))
    assert "现在几点？" in human.content, "原始请求要带上，否则无从判断是否已满足"
    assert "t1" in human.content
    assert "现在是 18:04" in human.content


@pytest.mark.asyncio
async def test_replanner_surfaces_failed_results():
    model = StructuredFake(result=ReplanDecision(should_continue=False))
    replanner = make_replanner(model)
    plan = Plan(tasks=[PlanTask(id="t1", description="查时间")])
    results = [TaskResult(task_id="t1", status="failed", error="工具炸了")]

    await replanner("req", plan, results)

    human = next(m for m in model.seen_messages if isinstance(m, HumanMessage))
    assert "failed" in human.content
    assert "工具炸了" in human.content
