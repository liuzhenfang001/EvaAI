"""记忆端点：增删改查与「没开这个功能」的答复。

这一组路由存在的理由是**用户得能自己抹掉模型记岔的东西**，所以「删掉之后真的没了」比「删掉了」
更要紧——每条写操作后面都跟着一次读回。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.routers import memory as memory_router
from app.tools import memory as memory_tools
from app.tools.memory import MemoryRuntime, make_extractor, make_render, start_memory
from evaharness.config.app_config import AppConfig, MemoryConfig, ModelConfig
from evaharness.memory import SqliteMemoryStore
from evaharness.runtime import RunOptions


def _client(runtime: MemoryRuntime | None) -> httpx.AsyncClient:
    """把记忆路由挂在一个裸 app 上（不起 lifespan、不读配置文件）。``None`` = 没开这个功能。"""
    app = FastAPI()
    app.state.memory = runtime
    app.include_router(memory_router.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def store(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.sqlite")
    yield store
    store.close()


def _runtime(store) -> MemoryRuntime:
    """开着的记忆运行时：库用测试的 store，渲染器用真配置。"""
    return MemoryRuntime(store=store, render=make_render(store, MemoryConfig(enabled=True)))


# --------------------------------------------------------------------------- #
# 启动期组装
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_disabled_returns_none(tmp_path):
    assert await start_memory(MemoryConfig(), db_path=tmp_path / "m.sqlite") is None


@pytest.mark.asyncio
async def test_enabled_builds_store_and_exposes_only_search(tmp_path):
    """三件工具都在代码里，但**只有 search 交给模型**（T21 起写入是被动的）。"""
    runtime = await start_memory(MemoryConfig(enabled=True), db_path=tmp_path / "m.sqlite")
    assert runtime is not None
    try:
        assert {tool.name for tool in runtime.tools} == {"memory_search"}
        assert (tmp_path / "m.sqlite").is_file()
    finally:
        await runtime.aclose()


def test_the_extractor_always_asks_for_thinking(monkeypatch):
    """抽取是后台推理活：固定开思考，**不跟用户的档位跑**（防有人顺手把它改成 off 省成本）。"""
    seen: dict[str, object] = {}

    class _Model:
        def with_structured_output(self, *args, **kwargs):
            return object()

    def fake_create(app_config, name=None, *, options=None, **kwargs):
        seen["name"] = name
        seen["options"] = options
        return _Model()

    monkeypatch.setattr(memory_tools, "create_chat_model", fake_create)
    app_config = AppConfig(
        config_version=1, models=[ModelConfig(name="m", model="m", api_key="sk-test")]
    )

    make_extractor(app_config, MemoryConfig(enabled=True, model="m"))

    assert seen == {"name": "m", "options": RunOptions(thinking="high")}


def test_the_renderer_reads_the_store_fresh_every_time(store):
    """不缓存渲染结果：刚 `memory_save` 的事，下一轮注入里就得有。"""
    render = make_render(store, MemoryConfig(enabled=True))
    assert render() == ""

    store.add_fact(content="偏好中文回答")

    assert "偏好中文回答" in render()


def test_the_db_path_is_made_from_scratch(tmp_path):
    """父目录不存在时也能建起来（``./data/memory.sqlite`` 这种配置）。"""
    path = Path(tmp_path) / "data" / "memory.sqlite"
    store = SqliteMemoryStore(path)
    try:
        assert path.is_file()
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_returns_profile_and_facts(store):
    store.set_profile("work_context", "做后端")
    store.add_fact(content="偏好中文回答", category="preference", confidence=0.9)

    async with _client(_runtime(store)) as client:
        response = await client.get("/memory")
    body = response.json()

    assert response.status_code == 200
    assert [block["section"] for block in body["profile"]] == ["work_context"]
    assert body["facts"][0]["content"] == "偏好中文回答"
    assert body["facts"][0]["category"] == "preference"
    assert "correction" in body["categories"]  # 页面的分类下拉靠它


@pytest.mark.asyncio
async def test_status_reports_counts(store):
    store.add_fact(content="一条")
    store.set_profile("work_context", "做后端")

    async with _client(_runtime(store)) as client:
        response = await client.get("/memory/status")

    assert response.json() == {"enabled": True, "facts": 1, "profile_blocks": 1}


@pytest.mark.asyncio
async def test_status_answers_even_when_disabled():
    """状态端点**不用** 409：页面正是靠它来知道该不该显示「未启用」的。"""
    async with _client(None) as client:
        response = await client.get("/memory/status")

    assert response.status_code == 200
    assert response.json() == {"enabled": False, "facts": 0, "profile_blocks": 0}


# --------------------------------------------------------------------------- #
# 写
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_adds_a_fact(store):
    async with _client(_runtime(store)) as client:
        response = await client.post("/memory/facts", json={"content": "偏好中文回答", "category": "preference"})

    assert response.status_code == 201
    assert response.json()["content"] == "偏好中文回答"
    assert [fact.content for fact in store.facts()] == ["偏好中文回答"]


@pytest.mark.asyncio
async def test_create_rejects_a_duplicate(store):
    body = {"content": "偏好中文回答"}
    async with _client(_runtime(store)) as client:
        await client.post("/memory/facts", json=body)
        response = await client.post("/memory/facts", json=body)

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_create_rejects_an_unknown_category(store):
    async with _client(_runtime(store)) as client:
        response = await client.post("/memory/facts", json={"content": "随便", "category": "vibe"})

    assert response.status_code == 400
    assert store.facts() == []


@pytest.mark.asyncio
async def test_patch_changes_only_what_is_given(store):
    fact_id = store.add_fact(content="偏好中文", category="preference", confidence=0.6).id

    async with _client(_runtime(store)) as client:
        response = await client.patch(f"/memory/facts/{fact_id}", json={"confidence": 0.95})

    assert response.status_code == 200
    assert response.json()["confidence"] == 0.95
    assert store.fact(fact_id).content == "偏好中文"


@pytest.mark.asyncio
async def test_patch_an_unknown_fact_is_404(store):
    async with _client(_runtime(store)) as client:
        response = await client.patch("/memory/facts/fact_nope", json={"content": "改成这个"})

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_removes_the_fact(store):
    fact_id = store.add_fact(content="偏好中文").id

    async with _client(_runtime(store)) as client:
        response = await client.delete(f"/memory/facts/{fact_id}")

    assert response.status_code == 204
    assert store.facts() == []


@pytest.mark.asyncio
async def test_delete_is_idempotent(store):
    """删完页面会刷新列表，竞态下不该报错——本来就没有也是 204。"""
    async with _client(_runtime(store)) as client:
        response = await client.delete("/memory/facts/fact_nope")

    assert response.status_code == 204


@pytest.mark.asyncio
async def test_clear_empties_everything(store):
    store.add_fact(content="一条")
    store.set_profile("work_context", "做后端")
    store.set_watermark("th-1", "msg-1")

    async with _client(_runtime(store)) as client:
        response = await client.delete("/memory")

    assert response.status_code == 204
    assert store.snapshot().empty is True
    assert store.watermark("th-1") is None


# --------------------------------------------------------------------------- #
# 没开这个功能
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_every_route_says_409_when_memory_is_disabled():
    """**409 而不是 404**：路由是静态注册的，``enabled`` 是运行期配置——「功能没开」与
    「没有这个端点」是两件事，混为一谈会让配置文件与代码互相甩锅。"""
    async with _client(None) as client:
        responses = [
            await client.get("/memory"),
            await client.post("/memory/facts", json={"content": "x"}),
            await client.patch("/memory/facts/fact_1", json={"content": "x"}),
            await client.delete("/memory/facts/fact_1"),
            await client.delete("/memory"),
        ]

    assert [response.status_code for response in responses] == [409] * 5
