"""契约测试：终止回复中间件。"""

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.runtime import Runtime

from evaharness.agents.middlewares.terminal_response import _FALLBACK_CONTENT, TerminalResponseMiddleware


def _state():
    return {
        "messages": [
            HumanMessage("hi"),
            AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "c1"}], id="a1"),
            ToolMessage(content="res", tool_call_id="c1"),
            AIMessage(content="", id="a2"),
        ]
    }


def test_first_empty_retries_with_jump_to_model():
    mw = TerminalResponseMiddleware()
    rt = Runtime(context={"thread_id": "t1"})
    out = mw.after_model(_state(), rt)
    assert out["jump_to"] == "model"
    assert any(isinstance(m, RemoveMessage) and getattr(m, "id", None) == "a2" for m in out["messages"])


def test_second_empty_falls_back():
    mw = TerminalResponseMiddleware()
    rt = Runtime(context={"thread_id": "t1"})
    mw.after_model(_state(), rt)  # 第 1 次重试
    out = mw.after_model(_state(), rt)  # 第 2 次兜底
    assert out["messages"][0].content == _FALLBACK_CONTENT
    assert out["messages"][0].additional_kwargs["evaharness_error_fallback"] is True


def test_visible_content_no_op():
    mw = TerminalResponseMiddleware()
    rt = Runtime(context={"thread_id": "t1"})
    state = {"messages": [HumanMessage("hi"), AIMessage(content="final answer")]}
    assert mw.after_model(state, rt) is None


def test_no_tool_result_no_op():
    mw = TerminalResponseMiddleware()
    rt = Runtime(context={"thread_id": "t1"})
    state = {"messages": [HumanMessage("hi"), AIMessage(content="")]}
    assert mw.after_model(state, rt) is None


def test_before_agent_resets_budget():
    mw = TerminalResponseMiddleware()
    rt = Runtime(context={"thread_id": "t1"})
    mw.after_model(_state(), rt)  # 用掉重试预算
    mw.before_agent(_state(), rt)  # 新 run 重置
    out = mw.after_model(_state(), rt)
    assert out["jump_to"] == "model"
