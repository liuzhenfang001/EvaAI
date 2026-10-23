"""被动抽取的写入侧：schema / prompt / 容错解析 / 落库 / 水位线。"""

from __future__ import annotations

import pytest

from evaharness.memory.capture import CapturedTurn
from evaharness.memory.store import SqliteMemoryStore
from evaharness.memory.update import (
    FactAdd,
    FactUpdate,
    MemoryUpdate,
    MemoryUpdater,
    ProfilePatch,
    apply_update,
    build_extraction_messages,
    tolerant_parse_update,
)


@pytest.fixture
def store(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.sqlite")
    yield store
    store.close()


def _turn(index: int, *, user: str | None = None, assistant: str | None = None) -> CapturedTurn:
    return CapturedTurn(
        user_text=user or f"第 {index} 轮的问题",
        assistant_text=assistant or f"第 {index} 轮的回答",
        last_message_id=f"a{index}",
    )


# --------------------------------------------------------------------------- #
# 落库
# --------------------------------------------------------------------------- #


def test_apply_adds_updates_removes_and_writes_profile(store):
    old = store.add_fact(content="用户在用 Python 3.10", category="context")
    stale = store.add_fact(content="用户住在北京")

    report = apply_update(
        store,
        MemoryUpdate(
            profile=[ProfilePatch(section="work_context", summary="做后端开发")],
            facts_add=[FactAdd(content="用户偏好中文回答", category="preference", confidence=0.9)],
            facts_update=[FactUpdate(id=old.id, content="用户在用 Python 3.12")],
            facts_remove=[stale.id],
        ),
        thread_id="th-1",
    )

    assert (report.profile, report.added, report.updated, report.removed) == (1, 1, 1, 1)
    assert [fact.content for fact in store.facts()] == [
        "用户偏好中文回答",
        "用户在用 Python 3.12",
    ]
    assert store.profile()[0].summary == "做后端开发"
    # 新加的那条带上来源会话（``facts()`` 新的在前）；被更新的旧事实保留原来的 source_thread。
    assert store.facts()[0].source_thread == "th-1"


def test_bad_items_are_skipped_without_losing_the_batch(store):
    report = apply_update(
        store,
        MemoryUpdate(
            facts_add=[
                FactAdd(content="正常的一条", category="preference"),
                FactAdd(content="分类写错了", category="mood"),
            ],
            facts_update=[FactUpdate(id="fact_missing", content="改不到")],
            facts_remove=["fact_missing"],
            profile=[ProfilePatch(section="mood", summary="不存在的块")],
        ),
        thread_id="th-1",
    )

    assert report.added == 1
    assert report.skipped == 4
    assert [fact.content for fact in store.facts()] == ["正常的一条"]


# --------------------------------------------------------------------------- #
# prompt
# --------------------------------------------------------------------------- #


def test_the_prompt_carries_existing_facts_with_ids_and_the_new_conversation(store):
    fact = store.add_fact(content="用户偏好中文回答")

    messages = build_extraction_messages(store, [_turn(1, user="以后都用中文", assistant="好的")])

    text = "\n".join(str(message.content) for message in messages)
    assert fact.id in text
    assert "用户偏好中文回答" in text
    assert "以后都用中文" in text
    assert "JSON" in text


def test_tolerant_parse_handles_fences_and_prose():
    fenced = '```json\n{"facts_add": [{"content": "偏好中文", "category": "preference"}]}\n```'
    prose = '好的，结果如下：{"facts_add": [{"content": "偏好中文", "category": "preference"}]} 完毕'

    assert tolerant_parse_update(fenced).facts_add[0].content == "偏好中文"
    assert tolerant_parse_update(prose).facts_add[0].content == "偏好中文"

    with pytest.raises(ValueError):
        tolerant_parse_update("完全不是 JSON")


# --------------------------------------------------------------------------- #
# updater + 水位线
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_successful_update_applies_and_advances_the_watermark(store):
    async def extract(_messages):
        return MemoryUpdate(facts_add=[FactAdd(content="用户偏好中文回答")])

    updater = MemoryUpdater(store, extract)
    await updater.update("th-1", [_turn(1)], "a1")

    assert [fact.content for fact in store.facts()] == ["用户偏好中文回答"]
    assert store.watermark("th-1") == "a1"


@pytest.mark.asyncio
async def test_an_already_processed_turn_is_not_extracted_again(store):
    store.set_watermark("th-1", "a1")
    calls: list[str] = []

    async def extract(_messages):
        calls.append("called")
        return MemoryUpdate()

    await MemoryUpdater(store, extract).update("th-1", [_turn(1)], "a1")

    assert calls == []


@pytest.mark.asyncio
async def test_turns_at_or_before_the_watermark_are_dropped(store):
    seen: list[str] = []

    async def extract(messages):
        text = "\n".join(str(message.content) for message in messages)
        seen.append(text)
        return MemoryUpdate()

    store.set_watermark("th-1", "a1")
    await MemoryUpdater(store, extract).update("th-1", [_turn(1), _turn(2)], "a2")

    assert "第 1 轮的问题" not in seen[-1]
    assert "第 2 轮的问题" in seen[-1]
    assert store.watermark("th-1") == "a2"


@pytest.mark.asyncio
async def test_a_failure_does_not_advance_the_watermark(store):
    async def boom(_messages):
        raise RuntimeError("模型挂了")

    updater = MemoryUpdater(store, boom, max_failures=3)
    await updater.update("th-1", [_turn(1)], "a1")

    assert store.watermark("th-1") is None


@pytest.mark.asyncio
async def test_repeated_failures_eventually_give_up_and_advance(store):
    async def boom(_messages):
        raise RuntimeError("模型挂了")

    updater = MemoryUpdater(store, boom, max_failures=2)
    await updater.update("th-1", [_turn(1)], "a1")
    await updater.update("th-1", [_turn(1)], "a1")

    assert store.watermark("th-1") == "a1"


@pytest.mark.asyncio
async def test_a_stale_result_is_not_written_back(store):
    async def extract(_messages):
        return MemoryUpdate(facts_add=[FactAdd(content="本该被丢弃")])

    updater = MemoryUpdater(store, extract)
    await updater.update("th-1", [_turn(1)], "a1", is_stale=lambda: True)

    assert store.facts() == []
    assert store.watermark("th-1") is None


@pytest.mark.asyncio
async def test_an_empty_window_still_advances_the_watermark(store):
    calls: list[str] = []

    async def extract(_messages):
        calls.append("called")
        return MemoryUpdate()

    await MemoryUpdater(store, extract).update("th-1", [], "a1")

    assert calls == []
    assert store.watermark("th-1") == "a1"
