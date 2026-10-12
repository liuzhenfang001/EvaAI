"""切分单测：markdown 标题面包屑、中文标点分隔、空文本。"""

from __future__ import annotations

from evaharness.knowledge.splitter import split_document

_CHINESE_ENDINGS = "。！？；，、"


def test_empty_text_yields_nothing():
    assert split_document("") == []
    assert split_document("   \n\n  ") == []


def test_markdown_breadcrumb_is_prefixed():
    """标题路径贴在每个片段开头——离了上下文也知道这段在讲什么。"""
    text = "# 安装\n\n## 前置条件\n\n需要 Python 3.12。\n"

    chunks = split_document(text, chunk_size=200, chunk_overlap=0)

    assert len(chunks) == 1
    assert chunks[0].startswith("安装 > 前置条件")
    assert "需要 Python 3.12" in chunks[0]


def test_plain_text_without_headers():
    chunks = split_document("就是一段普通文本。", chunk_size=200, chunk_overlap=0)
    assert chunks == ["就是一段普通文本。"]


def test_long_text_splits_on_chinese_punctuation():
    """长中文按中文句末断开，而不是从字中间劈开——这是显式传中文分隔符的意义。"""
    sentences = ["这是第一句话。", "这是第二句话！", "这是第三句话？", "这是第四句话；", "这是第五句话。"]
    text = "".join(sentence * 3 for sentence in sentences)

    chunks = split_document(text, chunk_size=30, chunk_overlap=0)

    assert len(chunks) > 1
    # 除最后一片外，每一片都应当停在一个中文句末标点上。
    for chunk in chunks[:-1]:
        assert chunk[-1] in _CHINESE_ENDINGS, chunk


def test_exclamation_and_question_are_boundaries():
    """``！？；`` 是官方 recipe **漏掉**的——它们同样是完整句界。"""
    text = "甲" * 30 + "！" + "乙" * 30 + "？" + "丙" * 30

    chunks = split_document(text, chunk_size=40, chunk_overlap=0)

    assert len(chunks) >= 2
    # 至少有一片以 `！` 或 `？` 收尾（说明它们被当成了可断点）。
    assert any(chunk[-1] in "！？" for chunk in chunks)


def test_chunk_size_is_respected():
    text = "字" * 500
    chunks = split_document(text, chunk_size=100, chunk_overlap=0)
    assert all(len(chunk) <= 100 for chunk in chunks)
