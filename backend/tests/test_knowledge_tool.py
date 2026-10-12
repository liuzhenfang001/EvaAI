"""检索工具单测：命中片段落 ``/workspace``、只回路径、模型能接着 ``read_file`` 读到。"""

from __future__ import annotations

import hashlib

import pytest
from tool_fakes import fake_runtime, lead_context

from evaharness.knowledge.store import Hit
from evaharness.knowledge.tool import KNOWLEDGE_SEARCH_NAME, make_knowledge_search
from evaharness.sandbox import SandboxLayout
from evaharness.sandbox.file_ops import read_file


class _StubRetriever:
    """只回一批固定命中——工具的单测不关心它内部怎么检索。"""

    def __init__(self, hits: list[Hit]) -> None:
        self._hits = hits
        self.queries: list[str] = []

    async def retrieve(self, query: str) -> list[Hit]:
        self.queries.append(query)
        return self._hits


async def _call(tool, runtime, query: str) -> str:
    fn = getattr(tool, "coroutine", None) or tool.func
    return await fn(runtime, query)


def _runtime(tmp_path):
    layout = SandboxLayout(root=tmp_path)
    return fake_runtime(lead_context(layout)), layout


@pytest.mark.asyncio
async def test_hits_are_written_and_only_paths_returned(tmp_path):
    hits = [
        Hit(source="guide/setup.md", ordinal=0, content="先装 Python 3.12。", distance=0.1),
        Hit(source="guide/run.md", ordinal=2, content="用 uv run 起服务。", distance=0.4),
    ]
    tool = make_knowledge_search(_StubRetriever(hits))
    runtime, _ = _runtime(tmp_path)

    out = await _call(tool, runtime, "怎么装")

    assert tool.name == KNOWLEDGE_SEARCH_NAME
    folder = hashlib.sha1("怎么装".encode()).hexdigest()[:8]
    assert f"/workspace/retrieved/{folder}/chunk_0.md" in out
    assert "先装 Python 3.12。" not in out  # 正文**不进**返回值，只给路径
    assert "guide/setup.md" in out


@pytest.mark.asyncio
async def test_written_file_is_readable_back(tmp_path):
    """落地的是真文件，且模型能用 read_file 按虚拟路径读回同一份内容。"""
    hits = [Hit(source="a.md", ordinal=0, content="关键结论在这里。", distance=0.0)]
    tool = make_knowledge_search(_StubRetriever(hits))
    runtime, layout = _runtime(tmp_path)

    await _call(tool, runtime, "q")

    folder = hashlib.sha1(b"q").hexdigest()[:8]
    virtual = f"/workspace/retrieved/{folder}/chunk_0.md"
    host = layout.session("th-1").resolve(virtual)
    assert host.is_file()
    assert "关键结论在这里。" in host.read_text(encoding="utf-8")

    assert "关键结论在这里。" in read_file.func(runtime, virtual)


@pytest.mark.asyncio
async def test_no_hits_returns_message(tmp_path):
    tool = make_knowledge_search(_StubRetriever([]))
    runtime, _ = _runtime(tmp_path)

    out = await _call(tool, runtime, "无关的问题")

    assert "No matching passage" in out


@pytest.mark.asyncio
async def test_same_query_is_idempotent(tmp_path):
    """同一句查询落在同一个目录（无状态、幂等），不会每调一次堆一个新目录。"""
    hits = [Hit(source="a.md", ordinal=0, content="内容。", distance=0.0)]
    tool = make_knowledge_search(_StubRetriever(hits))
    runtime, layout = _runtime(tmp_path)

    await _call(tool, runtime, "same")
    await _call(tool, runtime, "same")

    retrieved = layout.session("th-1").resolve("/workspace/retrieved")
    assert len(list(retrieved.iterdir())) == 1
