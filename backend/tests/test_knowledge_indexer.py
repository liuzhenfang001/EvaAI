"""索引器单测：**按文件增量**——只重嵌变动的文件，删除不留残影，非文本与坏文件不拦全流程。"""

from __future__ import annotations

import pytest
from knowledge_fakes import DIM, FakeEmbedder

from evaharness.fingerprint import ChangeDetector, InMemoryFingerprintStore, fingerprint
from evaharness.knowledge.indexer import KnowledgeIndexer
from evaharness.knowledge.store import KnowledgeStore


def _write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class _Rig:
    """一套跑完整「指纹 → diff → apply」的把子。"""

    def __init__(self, tmp_path):
        self.src = tmp_path / "src"
        self.src.mkdir()
        self.store = KnowledgeStore(tmp_path / "chroma")
        self.embedder = FakeEmbedder()
        self.fingerprints = InMemoryFingerprintStore()
        self.detector = ChangeDetector(self.fingerprints)

    async def __aenter__(self):
        await self.store.open()
        await self.store.ensure_recipe({"dimensions": DIM})
        self.indexer = KnowledgeIndexer(
            self.store, self.embedder, source_root=self.src, chunk_size=200, chunk_overlap=0
        )
        return self

    async def __aexit__(self, *exc):
        await self.store.close()

    def _snapshot(self):
        return fingerprint([self.src], mode="content")

    def detect(self):
        """算一次差异，并把当前指纹记成「上次」。"""
        snapshot = self._snapshot()
        changes = self.detector.diff("kb", snapshot)
        self.fingerprints.save("kb", snapshot)
        return changes

    async def apply(self):
        return await self.indexer.apply(self.detect())


@pytest.mark.asyncio
async def test_first_run_indexes_everything(tmp_path):
    async with _Rig(tmp_path) as rig:
        _write(rig.src, "a.md", "第一份文档。")
        _write(rig.src, "b.md", "第二份文档。")

        written = await rig.apply()

        assert written == 2
        assert await rig.store.sources() == ["a.md", "b.md"]


@pytest.mark.asyncio
async def test_changed_file_is_reembedded_alone(tmp_path):
    """改一个文件，只有它的 chunk 被重嵌——这就是「按文件重建」的全部意义。"""
    async with _Rig(tmp_path) as rig:
        _write(rig.src, "a.md", "甲文档。")
        _write(rig.src, "b.md", "乙文档。")
        await rig.apply()

        _write(rig.src, "a.md", "甲文档改过了。")
        rig.embedder.documents.clear()
        await rig.apply()

        embedded = "\n".join(rig.embedder.embedded_texts)
        assert "甲文档改过了" in embedded
        assert "乙文档" not in embedded  # 没动的文件不重嵌
        assert await rig.store.sources() == ["a.md", "b.md"]  # 另一份还在


@pytest.mark.asyncio
async def test_removed_file_is_purged(tmp_path):
    async with _Rig(tmp_path) as rig:
        _write(rig.src, "a.md", "甲。")
        _write(rig.src, "b.md", "乙。")
        await rig.apply()

        (rig.src / "b.md").unlink()
        await rig.apply()

        assert await rig.store.sources() == ["a.md"]


@pytest.mark.asyncio
async def test_new_file_is_added(tmp_path):
    async with _Rig(tmp_path) as rig:
        _write(rig.src, "a.md", "甲。")
        await rig.apply()

        _write(rig.src, "c.md", "丙。")
        await rig.apply()

        assert await rig.store.sources() == ["a.md", "c.md"]


@pytest.mark.asyncio
async def test_non_text_files_are_ignored(tmp_path):
    async with _Rig(tmp_path) as rig:
        _write(rig.src, "a.md", "甲。")
        (rig.src / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        await rig.apply()

        assert await rig.store.sources() == ["a.md"]
        assert not any("png" in text for text in rig.embedder.embedded_texts)


@pytest.mark.asyncio
async def test_emptied_file_is_purged(tmp_path):
    async with _Rig(tmp_path) as rig:
        _write(rig.src, "a.md", "有内容。")
        await rig.apply()

        _write(rig.src, "a.md", "   ")
        written = await rig.apply()

        assert written == 0
        assert await rig.store.sources() == []


@pytest.mark.asyncio
async def test_unreadable_file_is_skipped_loudly(tmp_path, caplog):
    """坏编码不该让整次重建停摆——点名跳过并记警告，其余文件照常进索引。"""
    async with _Rig(tmp_path) as rig:
        _write(rig.src, "good.md", "好的。")
        (rig.src / "bad.md").write_bytes(b"\xff\xfe\x00 not utf-8")

        with caplog.at_level("WARNING"):
            written = await rig.apply()

        assert written == 1
        assert "bad.md" in caplog.text
        assert await rig.store.sources() == ["good.md"]


@pytest.mark.asyncio
async def test_is_indexable():
    indexer = KnowledgeIndexer(None, None, source_root=".", suffixes=(".md", ".txt"))
    assert indexer.is_indexable("a.md")
    assert indexer.is_indexable("notes/x.TXT")
    assert not indexer.is_indexable("logo.png")
