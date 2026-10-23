"""三件记忆工具：记 / 查 / 删，以及「预期之内的否定结果怎么说」。

否定结果（重复、找不到、分类写错）返回**字符串**而不是抛异常是有意的：模型该读到「这条已经
记过了」并据此改主意，而不是拿到一个 error ToolMessage 去猜自己哪里做错了。
"""

from __future__ import annotations

import inspect

import pytest
from tool_fakes import fake_runtime, lead_context

from evaharness.memory import SqliteMemoryStore, make_memory_tools
from evaharness.memory.tool import MEMORY_FORGET_NAME, MEMORY_SAVE_NAME, MEMORY_SEARCH_NAME


@pytest.fixture
def store(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.sqlite")
    yield store
    store.close()


@pytest.fixture
def runtime():
    return fake_runtime(lead_context())


def _tool(store, name):
    return next(tool for tool in make_memory_tools(store) if tool.name == name)


async def _call(tool, runtime, *args, **kwargs) -> str:
    """直接调工具底下的协程——工具的单元测试不该为了它去跑一整张图。

    **只有 ``memory_save`` 吃运行上下文**（它要记「这条是哪次会话说的」），查询与删除都是
    纯粹的库操作。所以这里按签名决定要不要塞 ``runtime``，而不是一律塞三个。
    """
    fn = getattr(tool, "coroutine", None) or tool.func
    if "runtime" in inspect.signature(fn).parameters:
        return await fn(runtime, *args, **kwargs)
    return await fn(*args, **kwargs)


def test_the_three_tools_are_named_what_the_prompt_gates_on(store):
    names = {tool.name for tool in make_memory_tools(store)}
    assert names == {MEMORY_SAVE_NAME, MEMORY_SEARCH_NAME, MEMORY_FORGET_NAME}


@pytest.mark.asyncio
async def test_saving_reports_the_id_back(store, runtime):
    out = await _call(_tool(store, MEMORY_SAVE_NAME), runtime, "偏好中文回答", "preference", 0.9)

    saved = store.facts()
    assert len(saved) == 1
    assert saved[0].id in out  # 模型得拿到 id 才有机会日后忘掉它
    assert saved[0].category == "preference"


@pytest.mark.asyncio
async def test_a_saved_fact_records_which_conversation_it_came_from(store, runtime):
    await _call(_tool(store, MEMORY_SAVE_NAME), runtime, "在做 EvaAI")
    assert store.facts()[0].source_thread == "th-1"


@pytest.mark.asyncio
async def test_saving_the_same_thing_twice_says_so_instead_of_failing(store, runtime):
    tool = _tool(store, MEMORY_SAVE_NAME)
    await _call(tool, runtime, "偏好中文回答")

    out = await _call(tool, runtime, "偏好中文回答")

    assert "Already remembered" in out
    assert len(store.facts()) == 1


@pytest.mark.asyncio
async def test_an_unknown_category_is_refused_without_writing(store, runtime):
    out = await _call(_tool(store, MEMORY_SAVE_NAME), runtime, "随便一条", "vibe")

    assert "Unknown category" in out
    assert store.facts() == []


@pytest.mark.asyncio
async def test_a_blank_save_is_refused_without_writing(store, runtime):
    out = await _call(_tool(store, MEMORY_SAVE_NAME), runtime, "   ")

    assert "Could not save" in out
    assert store.facts() == []


@pytest.mark.asyncio
async def test_search_returns_ids_so_a_later_forget_is_possible(store, runtime):
    await _call(_tool(store, MEMORY_SAVE_NAME), runtime, "偏好中文回答", "preference")

    out = await _call(_tool(store, MEMORY_SEARCH_NAME), runtime, "中文")

    assert store.facts()[0].id in out
    assert "偏好中文回答" in out


@pytest.mark.asyncio
async def test_search_says_so_when_nothing_matches(store, runtime):
    out = await _call(_tool(store, MEMORY_SEARCH_NAME), runtime, "从来没有提过的事")
    assert "Nothing in long-term memory matches" in out


@pytest.mark.asyncio
async def test_search_can_be_filtered_by_start_time(store, runtime):
    """``since`` 按 ``updated_at`` 过滤；空 query + 时间是在问「最近更新了什么」。"""
    await _call(_tool(store, MEMORY_SAVE_NAME), runtime, "偏好中文回答", "preference")
    search = _tool(store, MEMORY_SEARCH_NAME)

    assert "偏好中文回答" in await _call(search, runtime, "", "", "2020-01-01")
    assert "Nothing" in await _call(search, runtime, "", "", "2999-01-01")


@pytest.mark.asyncio
async def test_a_bad_start_time_is_reported_instead_of_raising(store, runtime):
    out = await _call(_tool(store, MEMORY_SEARCH_NAME), runtime, "", "", "昨天")

    assert "Could not search that" in out


@pytest.mark.asyncio
async def test_search_can_be_narrowed_by_category(store, runtime):
    save = _tool(store, MEMORY_SAVE_NAME)
    await _call(save, runtime, "中文偏好", "preference")
    await _call(save, runtime, "中文约束", "constraint")

    out = await _call(_tool(store, MEMORY_SEARCH_NAME), runtime, "中文", "constraint")

    assert "中文约束" in out
    assert "中文偏好" not in out


@pytest.mark.asyncio
async def test_forget_removes_the_fact(store, runtime):
    await _call(_tool(store, MEMORY_SAVE_NAME), runtime, "偏好中文回答")
    fact_id = store.facts()[0].id

    out = await _call(_tool(store, MEMORY_FORGET_NAME), runtime, fact_id)

    assert "Forgot" in out
    assert store.facts() == []


@pytest.mark.asyncio
async def test_forgetting_an_unknown_id_is_not_an_error(store, runtime):
    out = await _call(_tool(store, MEMORY_FORGET_NAME), runtime, "fact_nope")
    assert "No memory with id" in out
