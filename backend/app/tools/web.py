"""联网工具：搜索（Tavily）与抓取（Jina Reader → 本地正文抽取 → Markdown）。

两个工具都是**工厂**（``use: app.tools.web:make_web_search``），不是裸的 ``@tool``：它们的
可调参数（条数、超时）在**构建期**绑进闭包，于是工具实例是自足的——调用期不读任何进程
全局。这与 ``models[].use`` + 构造参数、``journal.store`` + 构造参数是同一套约定；
配置里写错键名会让工厂抛 ``TypeError`` 而不是被静默忽略。

失败一律 raise（理由见 ``file_ops`` 模块 docstring）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import httpx
from langchain_core.tools import BaseTool, tool
from markdownify import markdownify
from readabilipy import simple_json_from_html_string
from tavily import AsyncTavilyClient

logger = logging.getLogger(__name__)

JINA_READER_URL = "https://r.jina.ai/"

WEB_FETCH_MAX_CHARS = 4096
"""``web_fetch`` 返回的正文上限（与 DeerFlow 同值）。

一整个网页塞进上下文会挤掉对话本身，而正文的价值集中在开头。
"""

_SEARCH_MAX_RESULTS_CAP = 10


def make_web_search(*, max_results: int = 5, timeout: float = 20, api_key: str | None = None) -> BaseTool:
    """建 ``web_search``。

    ``TAVILY_API_KEY`` **可选**：不填时 Tavily 走无 key 模式（实测可用，但有速率上限）。
    这里刻意不做「缺 key 就拒绝启动」——实测过无 key 是**能用**的，不是每次都失败，
    所以「模板配置必须先去申请一个第三方 key 才能把服务跑起来」就只是刁难第一次上手的人。
    真撞上速率上限时 Tavily 抛的异常会被 ``ToolErrorHandlingMiddleware`` 转成可见的工具错误。
    """
    key = api_key or os.getenv("TAVILY_API_KEY") or None
    cap = max(1, min(int(max_results), _SEARCH_MAX_RESULTS_CAP))

    @tool("web_search", parse_docstring=True)
    async def search(query: str) -> str:
        """Search the web for current information, news and facts.

        Returns JSON: {"query", "total_results", "results": [{"title", "url", "content"}]}.
        Use web_fetch on a returned url when you need the full page.

        Args:
            query: What to search for. Be specific for better results.
        """
        client = AsyncTavilyClient(api_key=key)
        try:
            response = await client.search(query, max_results=cap, timeout=timeout)
        finally:
            # 客户端每次调用现建现弃：工具实例跨请求共享（ADR-0011），在实例上缓存连接
            # 就是给并发留一个共享的可变对象。
            await client.close()

        results = [
            {
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "content": str(item.get("content") or ""),
            }
            for item in response.get("results") or []
        ]
        return json.dumps({"query": query, "total_results": len(results), "results": results}, ensure_ascii=False)

    return search


def make_web_fetch(
    *,
    timeout: int = 10,
    use_readability: bool = False,
    max_chars: int = WEB_FETCH_MAX_CHARS,
) -> BaseTool:
    """建 ``web_fetch``。

    ``use_readability`` 默认 **False**（DeerFlow 默认 True）：``readabilipy`` 走
    Readability.js 需要 Node **且需要它自带的 node_modules**——本机实测那份不存在，
    于是 ``use_readability=True`` 会在**请求路径上**触发一次 ``npm install``（要外网，
    失败还会每次重试）。纯 Python 那条路不用 Node、结果确定、可测，而 Jina 服务端本来
    就做过一次正文提取。想要 Readability.js 的抽取质量，把配置里的这一项打开即可。
    """
    limit = max(256, int(max_chars))

    @tool("web_fetch", parse_docstring=True)
    async def fetch(url: str) -> str:
        """Fetch a web page and return its main content as Markdown, truncated.

        Only fetch exact urls the user gave you or that came back from web_search. This
        tool cannot reach content behind a login.

        Args:
            url: An absolute URL including the scheme, e.g. https://example.com.
        """
        html = await _jina_crawl(url, timeout=timeout)
        title, markdown = await asyncio.to_thread(_extract_article, html, use_readability)
        body = markdown.strip()
        if len(body) > limit:
            body = body[:limit] + f"\n… (truncated at {limit} chars; fetch a more specific page for the rest)"
        return f"# {title}\n\n{body}" if title else body

    return fetch


async def _jina_crawl(url: str, *, return_format: str = "html", timeout: int = 10) -> str:
    """让 Jina Reader 去抓页面，拿回 HTML（它代我们处理了 JS 渲染与反爬）。"""
    headers = {"Content-Type": "application/json", "X-Return-Format": return_format, "X-Timeout": str(timeout)}
    if key := os.getenv("JINA_API_KEY"):
        headers["Authorization"] = f"Bearer {key}"

    try:
        async with httpx.AsyncClient(trust_env=True) as client:
            response = await client.post(JINA_READER_URL, headers=headers, json={"url": url}, timeout=timeout)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"could not reach Jina Reader: {type(exc).__name__}") from None

    if response.status_code != 200:
        raise RuntimeError(f"Jina Reader returned HTTP {response.status_code}")
    if not response.text.strip():
        raise RuntimeError("Jina Reader returned an empty body")
    return response.text


def _extract_article(html: str, use_readability: bool) -> tuple[str, str]:
    """提取 (标题, Markdown)。Readability.js 那条路不可用时降级到纯 Python。"""
    try:
        article: dict[str, Any] = simple_json_from_html_string(html, use_readability=use_readability)
    except Exception:
        # readabilipy 走 Node 时会抛 CalledProcessError / FileNotFoundError；用一个宽
        # except 是因为它内部的失败形态不止这两种，而这里**总有**纯 Python 可以退。
        if not use_readability:
            raise
        logger.warning("readabilipy 的 Node 路径不可用，降级到纯 Python 抽取", exc_info=True)
        article = simple_json_from_html_string(html, use_readability=False)

    title = str(article.get("title") or "").strip()
    return title, markdownify(article.get("content") or "")


__all__ = ["JINA_READER_URL", "WEB_FETCH_MAX_CHARS", "make_web_fetch", "make_web_search"]
