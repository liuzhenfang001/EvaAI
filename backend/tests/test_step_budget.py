"""契约测试：步数预算中间件。"""

from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from evaharness.agents.middlewares.step_budget import StepBudgetMiddleware


def _ai_with_tool():
    return AIMessage(content="", tool_calls=[{"name": "web_search", "args": {"query": "x"}, "id": "c1"}])


def _ai_text():
    return AIMessage(content="答完了")


def _state(msg):
    return {"messages": [msg]}


def test_steps_beyond_budget_strip_tool_calls():
    mw = StepBudgetMiddleware(max_steps=3)
    rt = Runtime(context={"thread_id": "t1"})
    for _ in range(3):
        assert mw.after_model(_state(_ai_with_tool()), rt) is None  # 前 3 步都在预算内
    out = mw.after_model(_state(_ai_with_tool()), rt)  # 第 4 步超限
    stripped = out["messages"][0]
    assert stripped.tool_calls == []
    assert "STEP BUDGET" in stripped.content


def test_text_replies_do_not_count():
    """没有 tool_calls 的模型响应 = 已经在收尾，不算一步，也不会超限。"""
    mw = StepBudgetMiddleware(max_steps=1)
    rt = Runtime(context={"thread_id": "t1"})
    assert mw.after_model(_state(_ai_text()), rt) is None


def test_before_agent_resets_the_bucket():
    """步数上限是一次运行内的概念，不是跨轮的——坐标跨轮复用是常态。"""
    mw = StepBudgetMiddleware(max_steps=2)
    rt = Runtime(context={"thread_id": "th-1:lead"})

    for _ in range(2):
        mw.before_agent(_state(_ai_with_tool()), rt)
        assert mw.after_model(_state(_ai_with_tool()), rt) is None

    # 第三轮：已被重置，不该再往上累加
    mw.before_agent(_state(_ai_with_tool()), rt)
    assert mw.after_model(_state(_ai_with_tool()), rt) is None


def test_lead_context_buckets_by_coordinate():
    """主 Agent 传的是带类型的上下文（``RunContext``），分桶得认坐标而不是兜底桶。"""
    from evaharness.runtime import RunContext

    mw = StepBudgetMiddleware(max_steps=1)
    rt = Runtime(context=RunContext(session_id="s", thread_id="s:lead", run_id="r1"))
    mw.after_model(_state(_ai_with_tool()), rt)

    assert set(mw._steps) == {"s:lead"}, "必须按坐标分桶，不能是 default"
