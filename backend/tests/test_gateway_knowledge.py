"""知识库接线的单测：投影、开关、缺 key、启动期后台重建与「无变化跳过」。全用假嵌入，不碰网络。"""

from __future__ import annotations

import pytest
from knowledge_fakes import DIM, FakeEmbedder

from app.tools import knowledge as knowledge_mod
from app.tools.knowledge import project_knowledge, start_knowledge
from evaharness.config.app_config import EmbeddingConfig, KnowledgeConfig


def _config(**overrides) -> KnowledgeConfig:
    base = {
        "enabled": True,
        "embedding": EmbeddingConfig(api_key="test-key", dimensions=DIM),
    }
    base.update(overrides)
    return KnowledgeConfig.model_validate(base)


def _paths(tmp_path):
    src = tmp_path / "knowledge"
    return {
        "source_root": src,
        "view_root": tmp_path / "knowledge-view",
        "persist_dir": tmp_path / "data" / "knowledge",
        "fingerprint_db_path": tmp_path / "data" / "fingerprints.sqlite",
    }


def _write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# 投影
# --------------------------------------------------------------------------- #


def test_project_mirrors_source(tmp_path):
    src = tmp_path / "knowledge"
    _write(src, "a.md", "甲")
    _write(src, "sub/b.md", "乙")
    view = tmp_path / "knowledge-view"

    project_knowledge(src, view)

    assert (view / "a.md").read_text(encoding="utf-8") == "甲"
    assert (view / "sub/b.md").read_text(encoding="utf-8") == "乙"


def test_project_drops_stale_files(tmp_path):
    """源是唯一真相：视图里多出来的东西（源已经删了的）必须跟着消失。"""
    src = tmp_path / "knowledge"
    _write(src, "a.md", "甲")
    view = tmp_path / "knowledge-view"
    _write(view, "ghost.md", "早该没了")

    project_knowledge(src, view)

    assert sorted(p.name for p in view.iterdir()) == ["a.md"]


def test_project_missing_source_leaves_no_view(tmp_path):
    view = tmp_path / "knowledge-view"
    _write(view, "stale.md", "旧")

    project_knowledge(tmp_path / "does-not-exist", view)

    assert not view.exists()


def test_project_rejects_unexpected_view_name(tmp_path):
    with pytest.raises(ValueError, match="knowledge-view"):
        project_knowledge(tmp_path, tmp_path / "elsewhere")


# --------------------------------------------------------------------------- #
# 启动期组装
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_disabled_returns_none(tmp_path):
    result = await start_knowledge(KnowledgeConfig(), **_paths(tmp_path))
    assert result is None


@pytest.mark.asyncio
async def test_enabled_without_key_raises(tmp_path):
    cfg = KnowledgeConfig.model_validate({"enabled": True})
    with pytest.raises(ValueError, match="api_key"):
        await start_knowledge(cfg, **_paths(tmp_path))


@pytest.mark.asyncio
async def test_enabled_builds_index_tool_and_view(tmp_path, monkeypatch):
    monkeypatch.setattr(knowledge_mod, "build_embedder", lambda _cfg: FakeEmbedder())
    paths = _paths(tmp_path)
    _write(paths["source_root"], "guide.md", "# 标题\n\n这是知识库里的一句话。")

    runtime = await start_knowledge(_config(), **paths)
    assert runtime is not None
    try:
        await runtime.build_task

        assert runtime.retriever.status == "ready"
        assert runtime.tool.name == "knowledge_search"
        assert (paths["view_root"] / "guide.md").is_file()

        hits = await runtime.retriever.retrieve("知识库")
        assert hits and "这是知识库里的一句话" in hits[0].content
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_unchanged_source_skips_rebuild(tmp_path, monkeypatch):
    """第二次启动指纹没变：不重嵌（库内容与片段数原样），检索器照样就绪。"""
    embedder = FakeEmbedder()
    monkeypatch.setattr(knowledge_mod, "build_embedder", lambda _cfg: embedder)
    paths = _paths(tmp_path)
    _write(paths["source_root"], "a.md", "稳定内容。")

    first = await start_knowledge(_config(), **paths)
    await first.build_task
    count_after_first = await first.store.count()
    await first.aclose()

    embedder.documents.clear()
    second = await start_knowledge(_config(), **paths)
    try:
        await second.build_task
        assert second.retriever.status == "ready"
        assert embedder.documents == []  # 没有重新嵌入
        assert await second.store.count() == count_after_first
    finally:
        await second.aclose()


@pytest.mark.asyncio
async def test_changed_source_rebuilds_only_that_file(tmp_path, monkeypatch):
    embedder = FakeEmbedder()
    monkeypatch.setattr(knowledge_mod, "build_embedder", lambda _cfg: embedder)
    paths = _paths(tmp_path)
    _write(paths["source_root"], "a.md", "甲。")
    _write(paths["source_root"], "b.md", "乙。")

    first = await start_knowledge(_config(), **paths)
    await first.build_task
    await first.aclose()

    _write(paths["source_root"], "a.md", "甲改过了。")
    embedder.documents.clear()
    second = await start_knowledge(_config(), **paths)
    try:
        await second.build_task
        embedded = "\n".join(embedder.embedded_texts)
        assert "甲改过了" in embedded
        assert "乙" not in embedded
    finally:
        await second.aclose()


@pytest.mark.asyncio
async def test_missing_source_dir_is_not_an_error(tmp_path, monkeypatch):
    """源目录不存在：响亮警告、索引为空但可用（不拦启动、不静默——日志里有话）。"""
    monkeypatch.setattr(knowledge_mod, "build_embedder", lambda _cfg: FakeEmbedder())
    paths = _paths(tmp_path)  # source_root 不建

    runtime = await start_knowledge(_config(), **paths)
    try:
        await runtime.build_task
        assert runtime.retriever.status == "ready"
        assert await runtime.retriever.retrieve("any") == []
        assert not paths["view_root"].exists()
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_failed_build_makes_retrieval_fail_loudly(tmp_path, monkeypatch):
    """构建失败 → 检索**可见地报错**，不是静默返回空。"""

    class _ExplodingEmbedder(FakeEmbedder):
        async def aembed_documents(self, texts):
            raise RuntimeError("嵌入服务挂了")

    monkeypatch.setattr(knowledge_mod, "build_embedder", lambda _cfg: _ExplodingEmbedder())
    paths = _paths(tmp_path)
    _write(paths["source_root"], "a.md", "内容。")

    runtime = await start_knowledge(_config(), **paths)
    try:
        await runtime.build_task
        assert runtime.retriever.status == "failed"

        from evaharness.knowledge import KnowledgeUnavailable

        with pytest.raises(KnowledgeUnavailable):
            await runtime.retriever.retrieve("q")
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_recipe_change_forces_full_rebuild(tmp_path, monkeypatch):
    """换了嵌入模型：源一个字没改，也必须整库重算（比对源指纹是不够的）。"""
    embedder = FakeEmbedder()
    monkeypatch.setattr(knowledge_mod, "build_embedder", lambda _cfg: embedder)
    paths = _paths(tmp_path)
    _write(paths["source_root"], "a.md", "稳定内容，一个字都没改。")

    first = await start_knowledge(_config(), **paths)
    await first.build_task
    await first.aclose()

    embedder.documents.clear()
    switched = _config(embedding=EmbeddingConfig(model="另一个模型", api_key="k", dimensions=DIM))
    second = await start_knowledge(switched, **paths)
    try:
        await second.build_task
        assert second.retriever.status == "ready"
        assert embedder.embedded_texts, "配方变了却没重嵌——旧模型的向量会静默留在库里"
    finally:
        await second.aclose()


@pytest.mark.asyncio
async def test_unchanged_recipe_and_source_skips_rebuild(tmp_path, monkeypatch):
    """配方与源都没变：不重嵌。"""
    embedder = FakeEmbedder()
    monkeypatch.setattr(knowledge_mod, "build_embedder", lambda _cfg: embedder)
    paths = _paths(tmp_path)
    _write(paths["source_root"], "a.md", "稳定内容。")

    first = await start_knowledge(_config(), **paths)
    await first.build_task
    await first.aclose()

    embedder.documents.clear()
    second = await start_knowledge(_config(), **paths)
    try:
        await second.build_task
        assert embedder.documents == []
    finally:
        await second.aclose()
