"""长期记忆的端到端：抽取落库 → 新会话注入一次 → 历史里查不到记忆块。

全部用假模型与假抽取器，不联网；队列用 3600 秒去抖，测试里直接 ``flush()``。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fakes import CallRecordingFake, collect
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.tools.memory import start_memory
from evaharness.agents.lead_agent import build_lead_middlewares, make_lead_agent
from evaharness.checkpoint import lead_thread_id
from evaharness.config.app_config import MemoryConfig
from evaharness.journal import RunJournal
from evaharness.journal.journal import journaled
from evaharness.journal.stores import MemoryJournalStore
from evaharness.memory import MEMORY_TAG, FactAdd, MemoryUpdate, ProfilePatch
from evaharness.runtime import MessagesEvent, run_agent


def _agent(runtime, *answers: str, saver=None):
    recorder = CallRecordingFake(messages=iter([AIMessage(content=text) for text in answers]))
    chain = build_lead_middlewares(recorder, memory=runtime.render, memory_capture=runtime.capture)
    return make_lead_agent(recorder, [], middleware=chain, checkpointer=saver or InMemorySaver()), recorder


async def _say(agent, text: str, thread_id: str, journal_store=None) -> list:
    events = run_agent(text, agent=agent, thread_id=thread_id)
    if journal_store is not None:
        events = journaled(events, RunJournal(journal_store, thread_id, flush_threshold=1))
    return await collect(events)


async def _state_messages(agent, thread_id: str) -> list:
    snapshot = await agent.aget_state({"configurable": {"thread_id": lead_thread_id(thread_id)}})
    return list(snapshot.values["messages"])


async def _extract(_messages):
    return MemoryUpdate(
        profile=[ProfilePatch(section="work_context", summary="做后端开发")],
        facts_add=[FactAdd(content="用户偏好中文回答", category="preference", confidence=0.9)],
    )


@pytest_asyncio.fixture
async def runtime(tmp_path):
    runtime = await start_memory(
        MemoryConfig(enabled=True, debounce_seconds=3600),
        db_path=tmp_path / "memory.sqlite",
        extractor=_extract,
    )
    assert runtime is not None
    yield runtime
    await runtime.aclose()


@pytest.mark.asyncio
async def test_a_conversation_is_extracted_into_profile_and_facts(runtime):
    agent, _ = _agent(runtime, "好的，我记住了")

    await _say(agent, "我偏好中文回答", "th-1")
    await runtime.queue.flush()

    assert [block.summary for block in runtime.store.profile()] == ["做后端开发"]
    assert [fact.content for fact in runtime.store.facts()] == ["用户偏好中文回答"]
    assert runtime.store.watermark("th-1") is not None


@pytest.mark.asyncio
async def test_a_new_thread_receives_the_memory_block_once(runtime):
    agent, recorder = _agent(runtime, "第一次回答", "第二次回答")

    await _say(agent, "我偏好中文回答", "th-1")
    await runtime.queue.flush()

    await _say(agent, "你好", "th-2")
    assert any("用户偏好中文回答" in str(message.content) for message in recorder.calls[-1])

    await _say(agent, "再说一句", "th-2")
    memory_messages = [
        message
        for message in await _state_messages(agent, "th-2")
        if getattr(message, "name", None) == MEMORY_TAG
    ]
    assert len(memory_messages) == 1


@pytest.mark.asyncio
async def test_only_memory_search_is_exposed_to_the_model(runtime):
    assert {tool.name for tool in runtime.tools} == {"memory_search"}


@pytest.mark.asyncio
async def test_the_injected_block_never_reaches_the_journal(runtime):
    journal_store = MemoryJournalStore()
    agent, _ = _agent(runtime, "好的", "你好")

    await _say(agent, "我偏好中文回答", "th-1")
    await runtime.queue.flush()
    await _say(agent, "你好", "th-2", journal_store=journal_store)

    entries = await journal_store.load("th-2")
    message_chunks = [
        str(entry.event.chunk.get("content", ""))
        for entry in entries
        if isinstance(entry.event, MessagesEvent)
    ]
    assert not any("<memory>" in chunk for chunk in message_chunks)


@pytest.mark.asyncio
async def test_clearing_memory_discards_pending_extraction(runtime):
    agent, _ = _agent(runtime, "好的")

    await _say(agent, "我偏好中文回答", "th-1")
    await runtime.clear()
    await runtime.queue.flush()

    assert runtime.store.facts() == []
    assert runtime.store.profile() == []
