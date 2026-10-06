"""契约测试：两个联网工具。

**测试一律不联网。** 打桩打在边界上——Tavily 打的是官方异步客户端、Jina 打的是
``httpx.AsyncClient.post``——所以被测的是我们自己的代码（结果整形、错误转换、截断），
而不是别人的服务。

``use_readability=False`` 是刻意的：``readabilipy`` 走 Node 那条路要它自带的
``node_modules``（本机没有），会触发 ``npm install``。这里只测纯 Python 那条。
"""

import asyncio
import json

import httpx
import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.tools import web

_PAGE = (
    "<html><head><title>测试页</title></head><body>"
    "<article><h1>标题</h1><p>这是正文第一段。</p></article>"
    "</body></html>"
)


def _tool_properties(tool) -> dict:  # noqa: ANN001
    return convert_to_openai_tool(tool)["function"]["parameters"].get("properties", {})


def _stub_post(monkeypatch, *, status: int = 200, text: str = _PAGE, seen: dict | None = None):
    async def fake_post(self, url, headers=None, json=None, timeout=None):  # noqa: ANN001, ANN003
        if seen is not None:
            seen.update({"url": url, "headers": headers or {}, "timeout": timeout})
        return httpx.Response(status, text=text, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


# --------------------------------------------------------------------------- #
# web_search
# --------------------------------------------------------------------------- #


def test_web_search_returns_the_normalized_json(monkeypatch):
    class FakeTavily:
        def __init__(self, api_key=None):  # noqa: ANN001, ARG002
            pass

        async def search(self, query, max_results=None, timeout=None):  # noqa: ANN001, ARG002
            return {"results": [{"title": "T", "url": "https://e.x", "content": "C"}]}

        async def close(self):
            pass

    monkeypatch.setattr(web, "AsyncTavilyClient", FakeTavily)
    tool = web.make_web_search(api_key="k")

    out = json.loads(asyncio.run(tool.ainvoke({"query": "天气"})))
    assert out["query"] == "天气"
    assert out["total_results"] == 1
    assert out["results"][0] == {"title": "T", "url": "https://e.x", "content": "C"}


def test_web_search_with_no_results_is_an_empty_list_not_an_error(monkeypatch):
    class FakeTavily:
        def __init__(self, api_key=None):  # noqa: ANN001, ARG002
            pass

        async def search(self, query, max_results=None, timeout=None):  # noqa: ANN001, ARG002
            return {"results": []}

        async def close(self):
            pass

    monkeypatch.setattr(web, "AsyncTavilyClient", FakeTavily)
    out = json.loads(asyncio.run(web.make_web_search(api_key="k").ainvoke({"query": "x"})))
    assert out["total_results"] == 0 and out["results"] == []


def test_web_search_without_a_key_falls_back_to_keyless_mode(monkeypatch):
    """**没有 key 也要能建出来**：Tavily 的无 key 模式实测可用（有速率上限）。

    做成「缺 key 拒绝启动」的话，模板配置就要求第一次上手的人先去申请一个第三方 key
    才能把服务跑起来——而收益只是少一种「搜索被限速」的情形。
    """
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    seen: dict = {}

    class FakeTavily:
        def __init__(self, api_key=None):  # noqa: ANN001
            seen["api_key"] = api_key

        async def search(self, query, max_results=None, timeout=None):  # noqa: ANN001, ARG002
            return {"results": []}

        async def close(self):
            pass

    monkeypatch.setattr(web, "AsyncTavilyClient", FakeTavily)
    tool = web.make_web_search()

    assert tool.name == "web_search"
    asyncio.run(tool.ainvoke({"query": "x"}))
    assert seen["api_key"] is None, "没 key 时要走到 Tavily 的无 key 模式，而不是空串"


def test_max_results_is_a_build_time_knob_not_a_model_knob():
    """配置是唯一来源——模型不该看见也不该能改它（工厂形态的主要收益）。"""
    tool = web.make_web_search(api_key="k", max_results=99)
    assert "max_results" not in _tool_properties(tool)


def test_the_search_schema_only_exposes_the_query():
    assert set(_tool_properties(web.make_web_search(api_key="k"))) == {"query"}


# --------------------------------------------------------------------------- #
# web_fetch
# --------------------------------------------------------------------------- #


def test_web_fetch_renders_markdown_with_the_title(monkeypatch):
    _stub_post(monkeypatch)
    out = asyncio.run(web.make_web_fetch(use_readability=False).ainvoke({"url": "https://example.com"}))

    assert out.startswith("# 测试页")
    assert "这是正文第一段。" in out


def test_web_fetch_asks_jina_for_html(monkeypatch):
    seen: dict = {}
    _stub_post(monkeypatch, seen=seen)
    asyncio.run(web.make_web_fetch(use_readability=False, timeout=7).ainvoke({"url": "https://example.com"}))

    assert seen["url"] == web.JINA_READER_URL
    assert seen["headers"]["X-Return-Format"] == "html"
    assert seen["headers"]["X-Timeout"] == "7"
    assert seen["timeout"] == 7


def test_the_jina_api_key_is_optional(monkeypatch):
    seen: dict = {}
    _stub_post(monkeypatch, seen=seen)
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    asyncio.run(web.make_web_fetch(use_readability=False).ainvoke({"url": "https://example.com"}))
    assert "Authorization" not in seen["headers"]

    monkeypatch.setenv("JINA_API_KEY", "jina-123")
    _stub_post(monkeypatch, seen=seen)
    asyncio.run(web.make_web_fetch(use_readability=False).ainvoke({"url": "https://example.com"}))
    assert seen["headers"]["Authorization"] == "Bearer jina-123"


def test_web_fetch_raises_on_a_non_200(monkeypatch):
    _stub_post(monkeypatch, status=503, text="nope")
    with pytest.raises(RuntimeError, match="503"):
        asyncio.run(web.make_web_fetch(use_readability=False).ainvoke({"url": "https://example.com"}))


def test_web_fetch_raises_on_an_empty_body(monkeypatch):
    _stub_post(monkeypatch, text="   ")
    with pytest.raises(RuntimeError, match="empty"):
        asyncio.run(web.make_web_fetch(use_readability=False).ainvoke({"url": "https://example.com"}))


def test_web_fetch_reports_a_connection_failure_as_a_runtime_error(monkeypatch):
    async def boom(self, url, headers=None, json=None, timeout=None):  # noqa: ANN001, ANN003
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    with pytest.raises(RuntimeError, match="Jina Reader"):
        asyncio.run(web.make_web_fetch(use_readability=False).ainvoke({"url": "https://example.com"}))


def test_web_fetch_truncates_the_body(monkeypatch):
    fat = "<html><head><title>T</title></head><body><article><p>" + ("字" * 9000) + "</p></article></body></html>"
    _stub_post(monkeypatch, text=fat)

    out = asyncio.run(web.make_web_fetch(use_readability=False, max_chars=512).ainvoke({"url": "https://e.x"}))
    assert "truncated" in out
    assert len(out) < 900


def test_the_fetch_schema_only_exposes_the_url():
    assert set(_tool_properties(web.make_web_fetch())) == {"url"}
