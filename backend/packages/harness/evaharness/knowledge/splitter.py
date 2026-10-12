"""切分：markdown 标题层级 + 中文感知的长度控制。

两件事一起做，顺序不能反：

1. **先按标题切**（:class:`~langchain_text_splitters.MarkdownHeaderTextSplitter`），把标题
   路径从正文里摘出来、放进 metadata，再把它重新贴回每个 chunk 的开头当面包屑
   （``h1 > h2``）。于是一个深埋在二级标题下的片段，离了上下文也知道自己在讲什么——这对
   检索质量是实打实的，因为向量只看得到喂进去的那段字。
2. **再按长度切**（:class:`~langchain_text_splitters.RecursiveCharacterTextSplitter`），并
   **显式传中文分隔符**。

**为什么中文分隔符必须显式传**（本片最容易踩的坑）：``RecursiveCharacterTextSplitter`` 的
默认分隔符是 ``["\\n\\n", "\\n", " ", ""]``——**纯英文 / 空白导向，没有一个中文标点**。而
``from_language(...)`` 里的 ``Language`` 枚举是**编程语言**（给代码切分用的），不是自然语言
——所以 LangChain 根本不提供中文自然语言分隔符，不传就只能在字符层面硬切，中文句子会被
从中间劈开。中文没有词间空格，按「词」切也不可靠，长度用**字符数**衡量反而最贴近直觉。
"""

from __future__ import annotations

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

_MD_HEADERS: list[tuple[str, str]] = [
    ("#", "h1"),
    ("##", "h2"),
    ("###", "h3"),
    ("####", "h4"),
]
"""要识别的标题层级。层级键用 ``h1``/``h2`` 而不是原样的 ``#``——它只是 metadata 的一个键。"""

SPLITTER_RECIPE = "markdown-cn-end-v1"
"""切分配方的版本号，进「索引配方」一起比对。

切分逻辑一改（分隔符表、标题处理、`keep_separator` 等），已入库的 chunk 边界就与新配方不符——
同一份源会切出不同的片段。把它编进版本号，改动时**自动触发整库重建**，不用靠人记得手动清索引。
改了这里的切分行为，就把版本号加一。
"""

CHINESE_SEPARATORS: list[str] = ["\n\n", "\n", "。", "！", "？", "；", "，", "、", " ", ""]
"""递归切分的中文分隔符，**从强到弱**。

官方 recipe 只给了 ``。，、`` 之类，**缺 ``！？；``**——而这三者在中文里同样是完整的句子边界，
漏掉它们会让感叹句 / 疑问句 / 分句被从中间切开。末尾的 ``""`` 是兜底：前面的都切不动时，
按字符硬切，保证一定收敛。
"""


def split_document(
    text: str,
    *,
    chunk_size: int = 800,
    chunk_overlap: int = 120,
) -> list[str]:
    """把一份文档切成若干片段。

    Args:
        text: 文档原文。markdown 与纯文本走同一条管线——纯文本里没有 ``#`` 标题，第一步
            自然产出「一整段无标题」，不吃亏。
        chunk_size / chunk_overlap: 长度上限与重叠。按**字符**算（中文一个字约一个 token）。

    Returns:
        片段列表，**有序**（文档顺序），空白片段已剔除。
    """
    sections = MarkdownHeaderTextSplitter(
        headers_to_split_on=_MD_HEADERS,
        strip_headers=True,
    ).split_text(text)
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=CHINESE_SEPARATORS,
        # 默认的 `keep_separator=True` 把分隔符留在**下一片**的开头（实测：一片以「。」起头），
        # 既难看又让嵌入多读一个孤立标点。`"end"` 把它留在**前一片**的末尾——句子以句号收尾，
        # 标点不丢，读起来就是正常文本。langchain-text-splitters 1.x 支持这个取值。
        keep_separator="end",
    )

    chunks: list[str] = []
    for section in sections:
        breadcrumb = " > ".join(
            str(section.metadata[key]) for _, key in _MD_HEADERS if key in section.metadata
        )
        for piece in splitter.split_text(section.page_content):
            body = piece.strip()
            if not body:
                continue
            chunks.append(f"{breadcrumb}\n\n{body}" if breadcrumb else body)
    return chunks


__all__ = ["CHINESE_SEPARATORS", "split_document"]
