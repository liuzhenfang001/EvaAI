"""建索引：把**变动过的源文件**切分、嵌入、写进向量库。

本模块是「按文件增量重建」的落点。它吃一份 :class:`~evaharness.fingerprint.detector.ChangeSet`：

- ``removed`` → 从库里删掉那个源文件的全部 chunk；
- ``added`` / ``modified`` → 重切 + 重嵌那个文件（``upsert_file`` 内部先删后插）。

于是「改了 10 个文件里的 1 个」的代价是**重嵌那 1 个**，而不是整库重算。删掉的文件也不会
留残影——因为删除也在差异里（这正是指纹要按文件、而不是按整棵树的原因）。

嵌入走一个 :class:`Embedder` 协议（只有 ``aembed_documents`` / ``aembed_query`` 两个异步方法），
:class:`~langchain_openai.OpenAIEmbeddings` 天然满足它——测试塞一个假实现即可**完全不碰网络**。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Protocol, runtime_checkable

from evaharness.fingerprint.detector import ChangeSet
from evaharness.knowledge.splitter import split_document
from evaharness.knowledge.store import KnowledgeStore

logger = logging.getLogger(__name__)

DEFAULT_SUFFIXES: tuple[str, ...] = (".md", ".markdown", ".txt")
"""本片索引的文本类型。图片 / PDF 等留给多模态那片（见 ``docs/research/rag-multimodal.md``）。"""


@runtime_checkable
class Embedder(Protocol):
    """嵌入模型的最小接口。``OpenAIEmbeddings``（含 DashScope 兼容端点）直接满足它。"""

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def aembed_query(self, text: str) -> list[float]: ...


class KnowledgeIndexer:
    """把源目录里的文本文件写进 :class:`~evaharness.knowledge.store.KnowledgeStore`。"""

    def __init__(
        self,
        store: KnowledgeStore,
        embedder: Embedder,
        *,
        source_root: Path,
        chunk_size: int = 800,
        chunk_overlap: int = 120,
        suffixes: tuple[str, ...] = DEFAULT_SUFFIXES,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.source_root = Path(source_root)
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.suffixes = suffixes

    def is_indexable(self, relative: str) -> bool:
        """这个相对路径的文本类型要不要进索引。指纹里会有**非文本**文件（图片等），跳过它们。"""
        return Path(relative).suffix.lower() in self.suffixes

    def _host_path(self, relative: str) -> Path:
        return self.source_root / relative

    async def rebuild_file(self, relative: str) -> int:
        """重切 + 重嵌一个文件，返回写入的 chunk 数（文件空了 / 读不了则为 0）。"""
        try:
            text = await asyncio.to_thread(self._host_path(relative).read_text, encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # 读不了就**点名跳过**（不抛）：一个坏文件不该让整次重建停摆。指纹随后仍会被保存，
            # 所以它要等下次变动才会再试——这条日志就是这个「为什么它没进索引」的答案。
            logger.warning("知识库跳过读不了的文件：%s（%s）", relative, exc)
            await self.store.delete_source(relative)
            return 0

        chunks = split_document(text, chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap)
        if not chunks:
            # 文件成了空的：把旧 chunk 清掉，别留上一版的内容。
            await self.store.delete_source(relative)
            return 0
        embeddings = await self.embedder.aembed_documents(chunks)
        return await self.store.upsert_file(relative, chunks, embeddings)

    async def apply(self, changes: ChangeSet) -> int:
        """按差异更新索引，返回这次写入的 chunk 总数。

        非文本文件的变动（图片等）会被跳过——它们不在本片的索引范围内，指纹里却如实记着。
        """
        written = 0
        for relative in changes.removed:
            await self.store.delete_source(relative)
        for relative in changes.changed:
            if not self.is_indexable(relative):
                continue
            written += await self.rebuild_file(relative)
        return written


__all__ = ["DEFAULT_SUFFIXES", "Embedder", "KnowledgeIndexer"]
