"""契约测试：循环检测中间件。"""

from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from evaharness.agents.middlewares.loop_detection import LoopDetectionMiddleware, _hash_tool_calls


def _ai():
    return AIMessage(content="", tool_calls=[{"name": "web_search", "args": {"query": "x"}, "id": "c1"}])


def _state():
    return {"messages": [_ai()]}


def test_hash_is_order_independent():
    a = [{"name": "a", "args": {"query": "x"}}, {"name": "b", "args": {"query": "y"}}]
    b = [{"name": "b", "args": {"query": "y"}}, {"name": "a", "args": {"query": "x"}}]
    assert _hash_tool_calls(a) == _hash_tool_calls(b)


def test_warns_after_threshold_then_injects():
    mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    rt = Runtime(context={"thread_id": "t1"})
    for _ in range(2):
        assert mw.after_model(_state(), rt) is None
    assert mw.after_model(_state(), rt) is None  # 第 3 次触发警告排队

    captured = {}

    def handler(request):
        captured["messages"] = request.messages
        return AIMessage(content="ok")

    req = ModelRequest(model=FakeListChatModel(responses=["ok"]), messages=[], runtime=rt)
    mw.wrap_model_call(req, handler)
    assert captured["messages"][-1].name == "loop_warning"


def test_hard_stop_strips_tool_calls():
    mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    rt = Runtime(context={"thread_id": "t1"})
    for _ in range(4):
        mw.after_model(_state(), rt)
    out = mw.after_model(_state(), rt)  # 第 5 次触发硬停
    stripped = out["messages"][0]
    assert stripped.tool_calls == []
    assert "FORCED STOP" in stripped.content
    assert mw.consume_stop_reason("t1") == "loop_capped"


def test_different_tool_calls_do_not_loop():
    mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    rt = Runtime(context={"thread_id": "t1"})

    def state_with(query):
        return {
            "messages": [
                AIMessage(content="", tool_calls=[{"name": "web_search", "args": {"query": query}, "id": "c1"}])
            ]
        }

    for query in ("a", "b", "c", "d"):
        assert mw.after_model(state_with(query), rt) is None
    assert mw.consume_stop_reason("t1") is None


# --------------------------------------------------------------------------- #
# 按运行重置（T8）
# --------------------------------------------------------------------------- #


def test_before_agent_resets_the_thread_bucket():
    """**同一个桶跨轮复用是常态，不该被判成循环。**

    lead agent 的 checkpoint 坐标是会话级的（``{会话}:lead``），一个会话从头到尾
    共用同一个桶。少了这个重置，「查一下时间」在第 3 轮就会触发循环警告、第 5 轮直接
    被剥掉 tool_calls 强行掐断——用户的正常提问会莫名其妙地答不出来。

    语义上也是对的：**循环是一次运行内的现象，不是跨轮的**。参照
    ``TerminalResponseMiddleware``，它同样在 ``before_agent`` 里重置自己的重试预算。
    """
    mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    rt = Runtime(context={"thread_id": "th-1:lead"})

    for _ in range(2):
        mw.before_agent(_state(), rt)
        assert mw.after_model(_state(), rt) is None

    # 第三轮：计数已被前两轮的重置清零，不该触发警告
    mw.before_agent(_state(), rt)
    assert mw.after_model(_state(), rt) is None

    captured = {}

    def handler(request):
        captured["messages"] = request.messages
        return AIMessage(content="ok")

    req = ModelRequest(model=FakeListChatModel(responses=["ok"]), messages=[], runtime=rt)
    mw.wrap_model_call(req, handler)
    assert captured["messages"] == [], "跨轮累计触发了循环警告"


def test_before_agent_leaves_other_threads_alone():
    """只清本线程的桶——并行运行的其他会话不该被牵连。"""
    mw = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    other = Runtime(context={"thread_id": "th-2:lead"})
    for _ in range(4):
        mw.after_model(_state(), other)

    mw.before_agent(_state(), Runtime(context={"thread_id": "th-1:lead"}))

    out = mw.after_model(_state(), other)  # 第 5 次，仍然硬停
    assert "FORCED STOP" in out["messages"][0].content


# --------------------------------------------------------------------------- #
# 分桶键的形状（T8 回归）
# --------------------------------------------------------------------------- #


def test_lead_context_buckets_by_coordinate_not_the_fallback():
    """主 Agent 传的是**带类型的上下文**（``LeadContext``），不是 dict。

    只认 dict 的实现会落进 ``"default"`` 兜底桶——所有会话、所有子 agent 共用一本账。
    T8 之前没暴露，是因为那时只有 Plan-Exec 一种路径、而它传的正好是 dict。
    """
    from evaharness.agents.lead_agent import LeadContext

    mw = LoopDetectionMiddleware()
    rt = Runtime(context=LeadContext(session_id="s", thread_id="s:lead", run_id="r1"))
    mw.after_model(_state(), rt)

    assert set(mw._history) == {"s:lead"}, "必须按坐标分桶，不能是 default"


def test_two_coordinates_do_not_share_a_ledger():
    """**这才是真正的失败模式**：两个会话互相累加，谁先撞上限谁先被掐断。

    并发跑两个会话时（或者主 Agent 派了子 agent 时），共用一个桶意味着一个会话的工具
    调用会算到另一个头上。这条在「按坐标分桶」被破坏时必然失败。
    """
    from evaharness.agents.lead_agent import LeadContext

    mw = LoopDetectionMiddleware(warn_threshold=2, hard_limit=3)
    first = Runtime(context=LeadContext(session_id="s", thread_id="s:lead", run_id="r1"))
    second = Runtime(context=LeadContext(session_id="t", thread_id="t:lead", run_id="r1"))

    for _ in range(3):
        mw.after_model(_state(), first)

    assert mw.after_model(_state(), second) is None, "另一个会话不该被牵连"


def test_a_subagent_does_not_reset_the_parent_ledger():
    """子 agent 的 ``before_agent`` 只该清它自己的桶。

    共用兜底桶时，主 Agent 派一次子 agent 就会把它正在累积的循环检测历史清空——
    循环检测因此被静默地缴械。
    """
    from evaharness.agents.lead_agent import LeadContext

    mw = LoopDetectionMiddleware(warn_threshold=2, hard_limit=3)
    parent = Runtime(context=LeadContext(session_id="s", thread_id="s:lead", run_id="r1"))
    child = Runtime(context=LeadContext(session_id="s", thread_id="s:r1:sub:c1", run_id="r1"))

    for _ in range(2):
        mw.after_model(_state(), parent)
    mw.before_agent(_state(), child)  # 子 agent 开始

    assert mw.after_model(_state(), parent) is not None, "父的账本被清空了"
