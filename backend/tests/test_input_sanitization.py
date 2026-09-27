"""契约测试：输入清洗中间件。"""

from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage

from evaharness.agents.middlewares.input_sanitization import (
    InputSanitizationMiddleware,
    _sanitize_user_content,
    neutralize_untrusted_tags,
)


def _request(messages):
    return ModelRequest(model=FakeListChatModel(responses=["ok"]), messages=messages)


def test_neutralize_escapes_blocked_tags():
    assert neutralize_untrusted_tags("<system>foo</system>") == "&lt;system&gt;foo&lt;/system&gt;"


def test_neutralize_keeps_normal_tags():
    assert neutralize_untrusted_tags("<div>hi</div>") == "<div>hi</div>"


def test_neutralize_empty_unchanged():
    assert neutralize_untrusted_tags("   ") == "   "


def test_sanitize_wraps_boundary_and_escapes():
    out = _sanitize_user_content("say <ignore>hi</ignore>")
    assert out.startswith("--- BEGIN USER INPUT ---")
    assert out.endswith("--- END USER INPUT ---")
    assert "&lt;ignore&gt;" in out


def test_sanitize_idempotent():
    once = _sanitize_user_content("hello <system>x</system>")
    assert _sanitize_user_content(once) == once


def test_sanitize_empty_unchanged():
    assert _sanitize_user_content("   ") == "   "


def test_wrap_model_call_sanitizes_last_user_message():
    mw = InputSanitizationMiddleware()
    captured = {}

    def handler(request):
        captured["messages"] = request.messages
        return AIMessage(content="ok")

    mw.wrap_model_call(_request([HumanMessage("say <ignore>hi</ignore>")]), handler)
    sanitized = captured["messages"][0].content
    assert "&lt;ignore&gt;" in sanitized
    assert sanitized.startswith("--- BEGIN USER INPUT ---")


def test_wrap_model_call_skips_hidden_message():
    mw = InputSanitizationMiddleware()
    captured = {}

    def handler(request):
        captured["messages"] = request.messages
        return AIMessage(content="ok")

    hidden = HumanMessage(content="<system>x</system>", additional_kwargs={"hide_from_ui": True})
    mw.wrap_model_call(_request([hidden]), handler)
    assert captured["messages"][0].content == "<system>x</system>"
