"""契约测试：LLM 错误处理中间件。"""

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage
from langgraph.errors import GraphBubbleUp

from evaharness.agents.middlewares.llm_error_handling import LLMErrorHandlingMiddleware, _classify_error


class _APIError(Exception):
    def __init__(self, message="", status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _request():
    return ModelRequest(model=FakeListChatModel(responses=["ok"]), messages=[])


def _mw(**kwargs):
    return LLMErrorHandlingMiddleware(retry_base_delay_ms=1, retry_cap_delay_ms=1, **kwargs)


def test_classify_retriable_500():
    retriable, reason = _classify_error(_APIError(status_code=500))
    assert retriable is True
    assert reason == "transient"


def test_classify_auth_not_retriable():
    retriable, reason = _classify_error(_APIError("invalid api key"))
    assert retriable is False
    assert reason == "auth"


def test_classify_quota_not_retriable():
    retriable, reason = _classify_error(_APIError("insufficient_quota exceeded"))
    assert retriable is False
    assert reason == "quota"


def test_retries_then_succeeds():
    mw = _mw(retry_max_attempts=3)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _APIError(status_code=500)
        return AIMessage(content="ok")

    result = mw.wrap_model_call(_request(), handler)
    assert result.content == "ok"
    assert calls["n"] == 3


def test_falls_back_after_retries():
    mw = _mw(retry_max_attempts=3)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise _APIError(status_code=500)

    result = mw.wrap_model_call(_request(), handler)
    assert result.additional_kwargs["evaharness_error_fallback"] is True
    assert result.additional_kwargs["error_reason"] == "transient"
    assert calls["n"] == 3


def test_non_retriable_no_retry():
    mw = _mw(retry_max_attempts=3)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise _APIError("invalid api key")

    result = mw.wrap_model_call(_request(), handler)
    assert result.additional_kwargs["error_reason"] == "auth"
    assert calls["n"] == 1


def test_graph_bubble_up_propagates():
    mw = _mw()

    def handler(request):
        raise GraphBubbleUp()

    with pytest.raises(GraphBubbleUp):
        mw.wrap_model_call(_request(), handler)
