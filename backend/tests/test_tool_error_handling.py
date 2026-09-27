"""契约测试：工具错误处理中间件。"""

import pytest
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.prebuilt.tool_node import ToolCallRequest

from evaharness.agents.middlewares.tool_error_handling import ToolErrorHandlingMiddleware


def _request(name="web_search", args=None, tool_call_id="c1"):
    return ToolCallRequest(
        tool_call={"name": name, "args": args or {"query": "x"}, "id": tool_call_id},
        tool=None,
        state={},
        runtime=None,
    )


def test_converts_exception_to_error_tool_message():
    mw = ToolErrorHandlingMiddleware()

    def handler(request):
        raise RuntimeError("boom")

    result = mw.wrap_tool_call(_request(), handler)
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert result.tool_call_id == "c1"
    assert result.name == "web_search"
    assert "RuntimeError" in result.content


def test_passes_through_success():
    mw = ToolErrorHandlingMiddleware()

    def handler(request):
        return ToolMessage(content="ok", tool_call_id="c1")

    result = mw.wrap_tool_call(_request(), handler)
    assert result.content == "ok"
    assert result.status != "error"


def test_missing_tool_call_id_fallback():
    mw = ToolErrorHandlingMiddleware()

    def handler(request):
        raise ValueError("x")

    result = mw.wrap_tool_call(_request(tool_call_id=None), handler)
    assert result.tool_call_id == "missing_tool_call_id"


def test_graph_bubble_up_propagates():
    mw = ToolErrorHandlingMiddleware()

    def handler(request):
        raise GraphBubbleUp()

    with pytest.raises(GraphBubbleUp):
        mw.wrap_tool_call(_request(), handler)
