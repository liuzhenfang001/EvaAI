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
