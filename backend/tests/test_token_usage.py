"""契约测试：用量日志中间件。"""

import logging

from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from evaharness.agents.middlewares.token_usage import TokenUsageMiddleware


def test_logs_usage(caplog):
    mw = TokenUsageMiddleware()
    state = {
        "messages": [
            AIMessage(content="hi", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
        ]
    }
    with caplog.at_level(logging.INFO):
        assert mw.after_model(state, Runtime()) is None
    assert "LLM token usage" in caplog.text
    assert "input=10" in caplog.text


def test_no_usage_no_log(caplog):
    mw = TokenUsageMiddleware()
    state = {"messages": [AIMessage(content="hi")]}
    with caplog.at_level(logging.INFO):
        assert mw.after_model(state, Runtime()) is None
    assert "LLM token usage" not in caplog.text


def test_non_ai_last_message_no_op():
    mw = TokenUsageMiddleware()
    state = {"messages": []}
    assert mw.after_model(state, Runtime()) is None
