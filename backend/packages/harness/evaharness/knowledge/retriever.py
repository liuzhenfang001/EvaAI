"""检索器：启动期常量，带一道「索引就绪」的闸门。

**为什么是启动期常量、不在请求里建**：每个请求都会重开一次向量库连接、重建一个嵌入客户端，
而连接与索引完全不随请求变化（见 ADR-0011 与 ADR-0017 补记二的同一条理由）。所以它像 MCP
工具那样，在 lifespan 构造一次、跨请求共享，必须无状态。

**为什么有「就绪」这道闸门**：索引重建被丢进**后台任务**跑，服务不等它——这是「别让一个
大库把启动拖住」的直接后果。代价是重启后的第一声检索可能落在索引还没建好的时候。闸门就是
处理这个的：``retrieve`` 先等就绪信号；构建若**失败**，则抛 :class:`KnowledgeUnavailable`
——**可见地失败**，而不是静默返回空（空结果与「还没建好」在模型眼里是一回事，那是本项目一贯
拒绝的失败形状）。
"""

from __future__ import annotations

import asyncio

from evaharness.knowledge.indexer import Embedder
from evaharness.knowledge.store import Hit, KnowledgeStore


class KnowledgeUnavailable(RuntimeError):
    """索引没建起来（构建失败）——检索无从谈起。"""


class KnowledgeRetriever:
    """向量检索 + 就绪闸门。启动期建一次，之后只读。"""

    def __init__(self, store: KnowledgeStore, embedder: Embedder, *, top_k: int = 4) -> None:
        self._store = store
        self._embedder = embedder
        self.top_k = top_k
        self._ready = asyncio.Event()
        self._error: BaseException | None = None
        self.status = "pending"
        """"pending" → "building" → "ready" / "failed"。给日志与测试看的，不作为控制流依据。"""

    def mark_building(self) -> None:
        self.status = "building"

    def mark_ready(self) -> None:
        """索引可用了（哪怕索引是空的——源目录为空也是一种「就绪」）。"""
        self.status = "ready"
        self._ready.set()

    def mark_failed(self, exc: BaseException) -> None:
        self.status = "failed"
        self._error = exc
        self._ready.set()

    async def wait_ready(self) -> None:
        """等索引就绪；构建失败则抛。裸等（不检索）也走这里。"""
        await self._ready.wait()
        if self._error is not None:
            raise KnowledgeUnavailable(
                f"知识库索引构建失败，暂时无法检索：{self._error}"
            ) from self._error

    async def retrieve(self, query: str) -> list[Hit]:
        """嵌入 query、做一次纯向量 top-k。"""
        await self.wait_ready()
        vector = await self._embedder.aembed_query(query)
        return await self._store.search(vector, self.top_k)


__all__ = ["KnowledgeRetriever", "KnowledgeUnavailable"]
