"""向量存储：ChromaDB（嵌入式持久化），按**文件**为重建单位，带**索引配方**校验。

**为什么是 Chroma**：它有一手维护的适配器、接口与元数据过滤最好用，嵌入式运行（一个目录、
零外部服务）。代价照实记：向量索引**常驻内存**（几百 MB~几 GB），且它自带一套存储（自己用
SQLite 存文档与元数据），与我们 `data/` 下的 checkpoint 不是同一个库。选型对照见
``docs/research/rag-knowledge-base.md`` §2 与 ADR-0022。

**重建单位是文件**：``upsert_file`` 先把某个源文件的全部旧 chunk 删掉、再插入新的，于是
「改了一个文件」的代价是**重嵌那一个文件**，而不是整库重算。chunk 的 id 是
``f"{source}::{ordinal}"``，元数据里带着 ``source``，所以按文件删除是一条 ``where`` 查询。

**索引配方（recipe）**：嵌入模型一换、维度一改、切分参数一动，**已有的向量就全部作废**——
语义空间变了（结果静默变差）或维度对不上（写入直接报错）。所以把「这份索引是怎么算出来的」
记在集合的元数据里，启动时比对：不一致就**丢弃整个集合重建**。见
:meth:`KnowledgeStore.ensure_recipe`。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import chromadb
from chromadb.config import Settings
from chromadb.errors import NotFoundError

RECIPE_KEY = "evaai_recipe"
"""配方在集合元数据里的键名（Chroma 的元数据值只能是标量，所以配方存成一段 JSON 字符串）。"""


@dataclass(frozen=True, slots=True)
class Hit:
    """一次检索命中。``distance`` 是平方 L2 距离（越小越近）。"""

    source: str
    ordinal: int
    content: str
    distance: float


class KnowledgeStore:
    """一个 Chroma 持久化集合。

    生命周期是显式的 ``open()`` / ``ensure_recipe()`` / ``close()``（也支持 ``async with``）：
    ``open`` 只建客户端（很轻），集合在 :meth:`ensure_recipe` 里按配方建或复用。

    Chroma 的嵌入客户端是**同步**的，所以每个调用都丢进工作线程（``asyncio.to_thread``）——
    别占着事件循环（「重建不拖慢主进程」那条要求的一半）。真正慢的是嵌入本身（网络），
    那在 :class:`~evaharness.knowledge.indexer.KnowledgeIndexer` 里是异步的。
    """

    def __init__(self, path: str | Path, *, collection: str = "knowledge") -> None:
        self.path = Path(path)
        self.collection_name = collection
        self._client: chromadb.ClientAPI | None = None
        self._collection: Any | None = None

    async def open(self) -> None:
        """建持久化客户端（幂等）。集合要到 :meth:`ensure_recipe` 才定。"""
        if self._client is not None:
            return
        self.path.mkdir(parents=True, exist_ok=True)
        self._client = await asyncio.to_thread(
            chromadb.PersistentClient,
            path=str(self.path),
            settings=Settings(anonymized_telemetry=False),
        )

    async def close(self) -> None:
        self._client = None
        self._collection = None

    async def __aenter__(self) -> KnowledgeStore:
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @property
    def client(self) -> chromadb.ClientAPI:
        if self._client is None:
            raise RuntimeError("KnowledgeStore 还没 open()")
        return self._client

    @property
    def collection(self) -> Any:
        if self._collection is None:
            raise RuntimeError("KnowledgeStore 还没 ensure_recipe()")
        return self._collection

    async def ensure_recipe(self, recipe: Mapping[str, Any]) -> bool:
        """确保集合按 ``recipe`` 建好；返回 ``True`` 表示**配方没变**（旧向量仍然有效）。

        配方不同（或集合还不存在）时就**删掉整个集合重建**、返回 ``False``——调用方据此做一次
        全量重建。这是「换了嵌入模型就得重算」那条要求的落点：光比对源目录指纹是不够的，
        因为源一个字没改，向量也已经作废了。
        """
        wanted = json.dumps(dict(recipe), sort_keys=True, ensure_ascii=False)

        def _sync() -> tuple[Any, bool]:
            try:
                existing = self.client.get_collection(self.collection_name)
            except NotFoundError:
                existing = None
            if existing is not None and (existing.metadata or {}).get(RECIPE_KEY) == wanted:
                return existing, True
            if existing is not None:
                # 配方变了：旧向量全部作废，删掉重建（不是「清空」，是连维度设定一起重来）。
                self.client.delete_collection(self.collection_name)
            created = self.client.create_collection(
                self.collection_name, metadata={RECIPE_KEY: wanted}
            )
            return created, False

        self._collection, matched = await asyncio.to_thread(_sync)
        return matched

    async def upsert_file(
        self, source: str, chunks: list[str], embeddings: list[list[float]]
    ) -> int:
        """把某个源文件的 chunk 整体换掉（先删旧、后插新），返回写入的 chunk 数。

        传空 ``chunks`` 等价于删掉这个文件的所有内容——调用方不必分两条路。
        """
        if len(chunks) != len(embeddings):
            raise ValueError(f"chunk 数与向量数不一致：{len(chunks)} vs {len(embeddings)}")

        await self.delete_source(source)
        if not chunks:
            return 0
        ids = [f"{source}::{ordinal}" for ordinal in range(len(chunks))]
        metadatas = [{"source": source, "ordinal": ordinal} for ordinal in range(len(chunks))]
        await asyncio.to_thread(
            self.collection.add,
            ids=ids,
            embeddings=embeddings,
            documents=chunks,
            metadatas=metadatas,
        )
        return len(chunks)

    async def delete_source(self, source: str) -> int:
        """删掉某个源文件的全部 chunk。返回删掉的行数。"""
        before = await self.count_source(source)
        if before:
            await asyncio.to_thread(self.collection.delete, where={"source": source})
        return before

    async def count_source(self, source: str) -> int:
        """某个源文件在库里的 chunk 数。"""
        got = await asyncio.to_thread(self.collection.get, where={"source": source}, include=[])
        return len(got["ids"])

    async def search(self, vector: list[float], k: int) -> list[Hit]:
        """纯向量 top-k。命中按距离升序。"""
        if k <= 0:
            return []
        result = await asyncio.to_thread(
            self.collection.query,
            query_embeddings=[vector],
            n_results=k,
            include=["documents", "metadatas", "distances"],
        )
        documents = result["documents"][0] if result["documents"] else []
        metadatas = result["metadatas"][0] if result["metadatas"] else []
        distances = result["distances"][0] if result["distances"] else []
        return [
            Hit(
                source=meta["source"],
                ordinal=int(meta["ordinal"]),
                content=document,
                distance=distance,
            )
            for document, meta, distance in zip(documents, metadatas, distances, strict=True)
        ]

    async def sources(self) -> list[str]:
        """库里有内容的源文件（相对路径），有序。"""
        got = await asyncio.to_thread(self.collection.get, include=["metadatas"])
        return sorted({meta["source"] for meta in got["metadatas"]})

    async def count(self) -> int:
        """chunk 总数。"""
        return await asyncio.to_thread(self.collection.count)


__all__ = ["RECIPE_KEY", "Hit", "KnowledgeStore"]
