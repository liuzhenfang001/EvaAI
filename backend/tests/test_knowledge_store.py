"""向量库单测：ChromaDB 的建集合、写入、按文件替换、删除、KNN 检索、**索引配方**校验。

**不碰网络**——向量是手搓的短向量，Chroma 不调任何嵌入函数（我们显式传 embeddings）。
"""

from __future__ import annotations

import pytest

from evaharness.knowledge.store import KnowledgeStore

_RECIPE = {"embedding_model": "fake", "dimensions": 4, "chunk_size": 100}


async def _store(tmp_path) -> KnowledgeStore:
    store = KnowledgeStore(tmp_path / "chroma")
    await store.open()
    await store.ensure_recipe(_RECIPE)
    return store


@pytest.mark.asyncio
async def test_upsert_and_search(tmp_path):
    store = await _store(tmp_path)
    try:
        written = await store.upsert_file("a.md", ["hello", "world"], [[1, 0, 0, 0], [0, 1, 0, 0]])

        assert written == 2
        assert await store.count() == 2
        assert await store.sources() == ["a.md"]

        hits = await store.search([1.0, 0.0, 0.0, 0.0], k=2)
        assert [hit.content for hit in hits] == ["hello", "world"]
        assert hits[0].source == "a.md" and hits[0].ordinal == 0
        assert hits[0].distance < hits[1].distance
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_upsert_replaces_the_whole_file(tmp_path):
    """按文件重建的核心：同一个 source 再写一次，旧 chunk 必须消失（不是并存）。"""
    store = await _store(tmp_path)
    try:
        await store.upsert_file("a.md", ["old-0", "old-1", "old-2"], [[1, 0, 0, 0]] * 3)
        await store.upsert_file("a.md", ["new-0"], [[1, 0, 0, 0]])

        assert await store.count() == 1
        assert await store.count_source("a.md") == 1
        hits = await store.search([1.0, 0, 0, 0], k=5)
        assert [hit.content for hit in hits] == ["new-0"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_delete_source(tmp_path):
    store = await _store(tmp_path)
    try:
        await store.upsert_file("a.md", ["x"], [[1, 0, 0, 0]])
        await store.upsert_file("b.md", ["y"], [[0, 1, 0, 0]])

        removed = await store.delete_source("a.md")

        assert removed == 1
        assert await store.sources() == ["b.md"]
        assert await store.count() == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_upsert_empty_chunks_clears(tmp_path):
    store = await _store(tmp_path)
    try:
        await store.upsert_file("a.md", ["x"], [[1, 0, 0, 0]])
        await store.upsert_file("a.md", [], [])
        assert await store.count() == 0
        assert await store.sources() == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_mismatched_lengths_raise(tmp_path):
    store = await _store(tmp_path)
    try:
        with pytest.raises(ValueError, match="不一致"):
            await store.upsert_file("a.md", ["x", "y"], [[1, 0, 0, 0]])
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_search_k_zero_returns_empty(tmp_path):
    store = await _store(tmp_path)
    try:
        await store.upsert_file("a.md", ["x"], [[1, 0, 0, 0]])
        assert await store.search([1.0, 0, 0, 0], k=0) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_delete_missing_source_is_noop(tmp_path):
    store = await _store(tmp_path)
    try:
        assert await store.delete_source("nope.md") == 0
        assert await store.count_source("nope.md") == 0
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_search_on_empty_collection_returns_empty(tmp_path):
    store = await _store(tmp_path)
    try:
        assert await store.search([1.0, 0, 0, 0], k=3) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reopen_sees_persisted_data(tmp_path):
    """关掉再开，数据还在——它是落盘的，不是内存态。"""
    store = await _store(tmp_path)
    await store.upsert_file("a.md", ["persisted"], [[1, 0, 0, 0]])
    await store.close()

    reopened = KnowledgeStore(tmp_path / "chroma")
    await reopened.open()
    try:
        assert await reopened.ensure_recipe(_RECIPE) is True
        assert await reopened.count() == 1
        assert (await reopened.search([1.0, 0, 0, 0], k=1))[0].content == "persisted"
    finally:
        await reopened.close()


# --------------------------------------------------------------------------- #
# 索引配方：换了嵌入模型 / 维度 / 切分参数，旧向量必须作废
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_recipe_first_time_reports_new(tmp_path):
    store = KnowledgeStore(tmp_path / "chroma")
    await store.open()
    try:
        assert await store.ensure_recipe(_RECIPE) is False  # 集合还不存在
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_unchanged_recipe_keeps_data(tmp_path):
    store = await _store(tmp_path)
    try:
        await store.upsert_file("a.md", ["x"], [[1, 0, 0, 0]])
        assert await store.ensure_recipe(_RECIPE) is True
        assert await store.count() == 1  # 数据没被动
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_changed_recipe_wipes_the_index(tmp_path):
    """**这条是「换模型就得重算」的护栏**：配方一变，旧向量一个都不该留。"""
    store = await _store(tmp_path)
    try:
        await store.upsert_file("a.md", ["old-vector"], [[1, 0, 0, 0]])

        matched = await store.ensure_recipe({**_RECIPE, "embedding_model": "另一个模型"})

        assert matched is False
        assert await store.count() == 0
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_dimension_change_is_accepted(tmp_path):
    """维度变了不该报错——整库重建后按新维度接着用。"""
    store = await _store(tmp_path)
    try:
        await store.upsert_file("a.md", ["x"], [[1, 0, 0, 0]])

        assert await store.ensure_recipe({**_RECIPE, "dimensions": 8}) is False
        await store.upsert_file("a.md", ["y"], [[1, 0, 0, 0, 0, 0, 0, 0]])

        assert (await store.search([1.0, 0, 0, 0, 0, 0, 0, 0], k=1))[0].content == "y"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_properties_before_open_raise(tmp_path):
    store = KnowledgeStore(tmp_path / "chroma")
    with pytest.raises(RuntimeError):
        _ = store.client
    with pytest.raises(RuntimeError):
        _ = store.collection
