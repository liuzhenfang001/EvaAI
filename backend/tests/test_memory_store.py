"""长期记忆的存储：三张表的读写、去重、校验、确定性排序。

**去重那一条是重点**：``memory_facts.content`` 的唯一索引是「同一条内容只记一遍」的**唯一**
防线——静默吞掉重复的话，注入里会出现两行一模一样的话，而用户看到的是助手变啰嗦了，不是
「存储出了错」。
"""

from __future__ import annotations

import pytest

from evaharness.memory import DuplicateFact, SqliteMemoryStore, UnknownFact
from evaharness.memory.schema import PROFILE_GROUPS


@pytest.fixture
def store(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.sqlite")
    yield store
    store.close()


# --------------------------------------------------------------------------- #
# 事实的基本读写
# --------------------------------------------------------------------------- #


def test_a_fact_round_trips(store):
    written = store.add_fact(content="偏好中文回答", category="preference", confidence=0.9)

    assert written.id.startswith("fact_")
    assert store.fact(written.id) == written
    assert [fact.content for fact in store.facts()] == ["偏好中文回答"]


def test_the_same_content_twice_is_rejected(store):
    store.add_fact(content="偏好中文回答")

    with pytest.raises(DuplicateFact):
        store.add_fact(content="偏好中文回答")


def test_surrounding_whitespace_does_not_hide_a_duplicate(store):
    """「偏好中文回答」与「 偏好中文回答 」是同一条——去重前先 strip。"""
    store.add_fact(content="偏好中文回答")

    with pytest.raises(DuplicateFact):
        store.add_fact(content="  偏好中文回答\n")


def test_a_fact_records_the_conversation_it_came_from(store):
    fact = store.add_fact(content="在做 EvaAI", source_thread="th-7")
    assert fact.source_thread == "th-7"


def test_blank_content_is_rejected(store):
    with pytest.raises(ValueError, match="不能为空"):
        store.add_fact(content="   ")


def test_an_unknown_category_is_rejected(store):
    with pytest.raises(ValueError, match="未知的事实分类"):
        store.add_fact(content="随便", category="vibe")


def test_confidence_outside_zero_to_one_is_rejected(store):
    with pytest.raises(ValueError, match="置信度"):
        store.add_fact(content="随便", confidence=1.5)


def test_facts_come_back_newest_first(store):
    first = store.add_fact(content="先记的")
    second = store.add_fact(content="后记的")

    assert [fact.id for fact in store.facts()] == [second.id, first.id]


def test_facts_can_be_filtered_by_category(store):
    store.add_fact(content="偏好甲", category="preference")
    store.add_fact(content="约束乙", category="constraint")

    assert [fact.content for fact in store.facts(category="constraint")] == ["约束乙"]


# --------------------------------------------------------------------------- #
# 改与删
# --------------------------------------------------------------------------- #


def test_update_patches_only_the_fields_given(store):
    fact = store.add_fact(content="偏好中文", category="preference", confidence=0.6)

    updated = store.update_fact(fact.id, confidence=0.95)

    assert updated.confidence == 0.95
    assert updated.content == "偏好中文"  # 没给的字段原样
    assert updated.category == "preference"
    assert updated.created_at == fact.created_at
    assert updated.updated_at >= fact.updated_at


def test_updating_an_unknown_fact_raises(store):
    with pytest.raises(UnknownFact):
        store.update_fact("fact_nope", content="改成这个")


def test_updating_into_an_existing_content_is_rejected(store):
    store.add_fact(content="甲")
    second = store.add_fact(content="乙")

    with pytest.raises(DuplicateFact):
        store.update_fact(second.id, content="甲")


def test_forget_removes_it_and_reports_whether_anything_went(store):
    fact = store.add_fact(content="临时记的")

    assert store.forget_fact(fact.id) is True
    assert store.fact(fact.id) is None
    # 幂等：再删一次不算错，只是「什么都没删掉」。
    assert store.forget_fact(fact.id) is False


# --------------------------------------------------------------------------- #
# 检索
# --------------------------------------------------------------------------- #


def test_search_matches_a_substring_and_ranks_by_confidence(store):
    store.add_fact(content="偏好中文回答", confidence=0.6)
    store.add_fact(content="回答要用中文，别夹英文", confidence=0.9)

    hits = store.search_facts("中文")

    assert [fact.content for fact in hits] == ["回答要用中文，别夹英文", "偏好中文回答"]


def test_search_can_be_narrowed_by_category(store):
    store.add_fact(content="中文偏好", category="preference")
    store.add_fact(content="中文约束", category="constraint")

    assert [fact.content for fact in store.search_facts("中文", category="constraint")] == ["中文约束"]


def test_search_treats_a_percent_sign_as_a_literal(store):
    """``%`` 是 LIKE 的通配符——不转义的话「100%」会匹配到任何东西。"""
    store.add_fact(content="可用率要 100%")
    store.add_fact(content="可用率要 99")

    assert [fact.content for fact in store.search_facts("100%")] == ["可用率要 100%"]


def test_an_empty_query_matches_nothing_rather_than_everything(store):
    store.add_fact(content="随便一条")

    assert store.search_facts("") == []
    assert store.search_facts("   ") == []


def test_search_respects_the_limit(store):
    for index in range(5):
        store.add_fact(content=f"中文第 {index} 条")

    assert len(store.search_facts("中文", limit=2)) == 2


class _Clock:
    """可控时钟：``since`` 靠 ``updated_at`` 过滤，同秒内写入必须能区分开。"""

    def __init__(self, value: str) -> None:
        self.value = value

    def __call__(self) -> str:
        return self.value


def test_search_filters_by_update_time(tmp_path):
    clock = _Clock("2026-10-01T00:00:00+00:00")
    store = SqliteMemoryStore(tmp_path / "m.sqlite", now=clock)
    try:
        older = store.add_fact(content="很早以前的事")
        clock.value = "2026-10-10T00:00:00+00:00"
        store.update_fact(older.id, content="最近改过的事")
        clock.value = "2026-10-20T00:00:00+00:00"
        store.add_fact(content="最近新增的事")

        hits = store.search_facts("", since="2026-10-05")
    finally:
        store.close()

    assert [fact.content for fact in hits] == ["最近新增的事", "最近改过的事"]


def test_search_accepts_date_only_and_z_suffix(store):
    store.add_fact(content="一条事实")

    assert len(store.search_facts("", since="2020-01-01")) == 1
    assert len(store.search_facts("", since="2020-01-01T00:00:00Z")) == 1
    assert len(store.search_facts("", since="2999-01-01")) == 0


def test_an_invalid_since_raises_a_readable_error(store):
    with pytest.raises(ValueError, match="看不懂这个时间"):
        store.search_facts("", since="昨天")


def test_an_empty_query_with_since_is_not_treated_as_match_everything(store):
    """只给时间是在问「最近更新了什么」；两者都空才是「什么都别返回」。"""
    store.add_fact(content="一条事实")

    assert len(store.search_facts("", since="2020-01-01")) == 1
    assert store.search_facts("", since="") == []


# --------------------------------------------------------------------------- #
# 画像
# --------------------------------------------------------------------------- #


def test_profile_blocks_come_back_in_canonical_order(store):
    """写入顺序打乱，读出来仍按六块的规范顺序——注入的字节稳定性靠这条。"""
    store.set_profile("long_term_background", "长期背景")
    store.set_profile("work_context", "做后端")
    store.set_profile("top_of_mind", "在写 EvaAI")

    assert [block.section for block in store.profile()] == ["work_context", "top_of_mind", "long_term_background"]


def test_writing_the_same_profile_block_overwrites(store):
    store.set_profile("work_context", "旧的工作描述")
    store.set_profile("work_context", "新的工作描述")

    assert len(store.profile()) == 1
    assert store.profile()[0].summary == "新的工作描述"


def test_an_unknown_profile_section_is_rejected(store):
    with pytest.raises(ValueError, match="未知的画像块"):
        store.set_profile("mood", "还行")


def test_a_blank_profile_summary_is_rejected(store):
    """空摘要没有意义——「清掉一块」是一个显式动作（``delete_profile``），不是写空串。"""
    with pytest.raises(ValueError, match="不能为空"):
        store.set_profile("work_context", "  ")


def test_delete_profile_reports_whether_anything_went(store):
    store.set_profile("work_context", "做后端")

    assert store.delete_profile("work_context") is True
    assert store.delete_profile("work_context") is False


def test_every_declared_section_is_a_known_one(store):
    """schema 里声明的六块必须都能写——两份清单不许漂移。"""
    for group in PROFILE_GROUPS.values():
        for section in group:
            store.set_profile(section.value, "x")

    assert len(store.profile()) == 6


# --------------------------------------------------------------------------- #
# 水位线、快照、清空
# --------------------------------------------------------------------------- #


def test_a_watermark_defaults_to_none_and_round_trips(store):
    assert store.watermark("th-1") is None

    store.set_watermark("th-1", "msg-9")

    assert store.watermark("th-1") == "msg-9"
    assert store.watermark("th-2") is None  # 每个会话各记各的


def test_a_snapshot_is_empty_until_something_is_written(store):
    assert store.snapshot().empty is True

    store.set_profile("work_context", "做后端")
    assert store.snapshot().empty is False


def test_clear_removes_facts_profile_and_watermarks(store):
    store.add_fact(content="一条")
    store.set_profile("work_context", "做后端")
    store.set_watermark("th-1", "msg-1")

    store.clear()

    assert store.snapshot().empty is True
    assert store.watermark("th-1") is None


def test_data_survives_reopening_the_file(tmp_path):
    """落盘就是落盘——关掉再打开，记忆还在（「跨会话」的前提）。"""
    path = tmp_path / "memory.sqlite"
    first = SqliteMemoryStore(path)
    first.add_fact(content="偏好中文回答")
    first.close()

    second = SqliteMemoryStore(path)
    try:
        assert [fact.content for fact in second.facts()] == ["偏好中文回答"]
    finally:
        second.close()
