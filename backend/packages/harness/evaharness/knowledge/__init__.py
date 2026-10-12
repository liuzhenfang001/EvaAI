"""知识库：切分 → 嵌入 → 向量存储 → 检索工具。

**机制在 harness，策略在 app**（与沙箱同一条分界，见 ADR-0020）。这一层提供的是通用的
「文本进、片段出」的机器：

- :mod:`~evaharness.knowledge.splitter` —— markdown 标题层级 + 中文分隔符的切分；
- :mod:`~evaharness.knowledge.store` —— ChromaDB 向量库（按文件为重建单位 + 索引配方校验）；
- :mod:`~evaharness.knowledge.indexer` —— 按 :class:`~evaharness.fingerprint.detector.ChangeSet` 增量重建；
- :mod:`~evaharness.knowledge.retriever` —— 启动期常量 + 就绪闸门；
- :mod:`~evaharness.knowledge.tool` —— 命中落 ``/workspace``、只回路径的检索工具。

「源目录在哪、库文件在哪、用哪个嵌入模型、切多大」都是**部署策略**，由 app 从配置里读了注入。
"""

from __future__ import annotations

from evaharness.knowledge.indexer import DEFAULT_SUFFIXES, Embedder, KnowledgeIndexer
from evaharness.knowledge.retriever import KnowledgeRetriever, KnowledgeUnavailable
from evaharness.knowledge.splitter import CHINESE_SEPARATORS, SPLITTER_RECIPE, split_document
from evaharness.knowledge.store import RECIPE_KEY, Hit, KnowledgeStore
from evaharness.knowledge.tool import (
    KNOWLEDGE_SEARCH_NAME,
    RETRIEVED_ROOT,
    make_knowledge_search,
)

__all__ = [
    "CHINESE_SEPARATORS",
    "DEFAULT_SUFFIXES",
    "KNOWLEDGE_SEARCH_NAME",
    "RECIPE_KEY",
    "RETRIEVED_ROOT",
    "SPLITTER_RECIPE",
    "Embedder",
    "Hit",
    "KnowledgeIndexer",
    "KnowledgeRetriever",
    "KnowledgeStore",
    "KnowledgeUnavailable",
    "make_knowledge_search",
    "split_document",
]
