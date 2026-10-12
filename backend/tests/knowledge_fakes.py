"""知识库测试的共用替身：一个**完全不碰网络**的嵌入实现。

harness 侧的切分 / 存储 / 索引 / 检索单测都用它，所以整片测试不需要 ``DASHSCOPE_API_KEY``，
也不出一趟网。向量由文本的 sha256 决定，因而**确定性**——同样的文本永远得到同一个向量，
两段文本的向量也不同，够用来验证「谁进了库、检索排第一的是谁」。
"""

from __future__ import annotations

import hashlib

DIM = 8
"""假向量的维度。测试配置里的 ``embedding.dimensions`` 必须与它一致（要建对表）。"""


class FakeEmbedder:
    """:class:`~evaharness.knowledge.indexer.Embedder` 协议的内存实现。"""

    def __init__(self, dim: int = DIM) -> None:
        self.dim = dim
        self.documents: list[list[str]] = []
        """每一次 ``aembed_documents`` 收到的文本批次——用来断言「只重嵌了变动的文件」。"""
        self.queries: list[str] = []

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        self.documents.append(list(texts))
        return [self.vector(text) for text in texts]

    async def aembed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return self.vector(text)

    def vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [digest[i] / 255.0 for i in range(self.dim)]

    @property
    def embedded_texts(self) -> list[str]:
        """所有被嵌入过的文本（摊平），用于「哪些 chunk 真的被重嵌了」的断言。"""
        return [text for batch in self.documents for text in batch]


__all__ = ["DIM", "FakeEmbedder"]
