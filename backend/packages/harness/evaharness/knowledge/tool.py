"""检索工具：命中片段写进 ``/workspace``，只把**路径**回给模型。

这是官方 Deep Agents 那套「retrieve, offload, and delegate」的形状——检索到的 chunk 不留在
编排者的上下文里，而是落进文件系统，模型（或它派出的子 agent）需要时再去读。好处是**上下文
预算按需**：不检索就没有任何成本，检索了也只回几行路径，正文从不无条件地顶进上下文。

**为什么结果落 ``/workspace`` 而不是 ``/output``**：``/output`` 是「交给用户的成品」，
``present_files`` 只放行它；检索命中的中间材料显然不是交付物——它是模型自己要看的东西，
所以进工作台。而且工作台与会话容器是同一份文件，模型用 ``read_file`` 读、在容器里 ``cat``
读，看到的是同一个文件。

**工具实例跨请求共享**（ADR-0011），所以它自己**不持有任何请求级状态**：写哪个目录由运行
上下文里的会话 id 决定，目录名由 query 的哈希决定（同一句查询落在同一个目录，幂等、无状态）。
"""

from __future__ import annotations

import hashlib
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool

from evaharness.knowledge.retriever import KnowledgeRetriever
from evaharness.runtime import sandbox_layout, session_id

KNOWLEDGE_SEARCH_NAME = "knowledge_search"
"""工具名。也是提示词决定要不要拼「知识库」那一段的依据。"""

RETRIEVED_ROOT = "/workspace/retrieved"
"""命中片段落地的目录。放在工作台下的一个专属子目录里，便于模型一眼认出「这是检索来的」。"""


def make_knowledge_search(retriever: KnowledgeRetriever) -> BaseTool:
    """建检索工具。``retriever`` 是**启动期常量**，在构造期绑进闭包（ADR-0012 §2）。"""

    @tool(KNOWLEDGE_SEARCH_NAME, parse_docstring=True)
    async def knowledge_search(runtime: ToolRuntime[Any], query: str) -> str:
        """Search the local knowledge base for passages relevant to a question.

        Matching passages are written under /workspace and only their paths are returned, so
        read the returned files to see what they actually say.

        Args:
            query: What to look for, in natural language.
        """
        hits = await retriever.retrieve(query)
        if not hits:
            return "No matching passage was found in the knowledge base."

        box = sandbox_layout(runtime).session(session_id(runtime))
        folder = f"{RETRIEVED_ROOT}/{hashlib.sha1(query.encode('utf-8')).hexdigest()[:8]}"

        lines: list[str] = []
        for index, hit in enumerate(hits):
            target = box.resolve(f"{folder}/chunk_{index}.md", write=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            header = f"<!-- source: {hit.source} · chunk {hit.ordinal} -->\n\n"
            target.write_text(header + hit.content, encoding="utf-8")
            lines.append(f"- {box.display(target)} (from: {hit.source})")

        return f"Found {len(hits)} matching passage(s). Read them with read_file:\n" + "\n".join(lines)

    return knowledge_search


__all__ = ["KNOWLEDGE_SEARCH_NAME", "RETRIEVED_ROOT", "make_knowledge_search"]
