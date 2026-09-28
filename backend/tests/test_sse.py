"""SSE 帧格式化契约测试。"""

from app.gateway.sse import format_sse


def test_format_sse_null_data():
    assert format_sse("end", None) == "event: end\ndata: null\n\n"


def test_format_sse_json_data():
    assert format_sse("messages", {"content": "Hi"}) == 'event: messages\ndata: {"content": "Hi"}\n\n'


def test_format_sse_keeps_unicode():
    assert format_sse("messages", {"content": "你好"}) == 'event: messages\ndata: {"content": "你好"}\n\n'


def test_format_sse_with_event_id():
    assert format_sse("end", None, event_id="abc-123") == "event: end\ndata: null\nid: abc-123\n\n"
