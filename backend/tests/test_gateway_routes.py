"""两条 chat 路由的接线：谁跑什么、首尾帧是什么、有没有记账、每请求建几张图。

``test_gateway_chat.py`` 直接把生成器喂给 ``sse_consumer``，绕过了 FastAPI 这一层；
这里补上被绕过的那一段——**接错线是这一类改动最容易犯又最难发现的错**（比如默认
路由还指着封存的 Plan-Exec），而且它不会让任何单元测试变红。

**注入契约是 ``app.state.agent_factory``**（不是图）：T9 起图是请求级产物，测试要能
看见「每个请求各建了一张」。任何挂 ``chat.router`` 的裸 app 都必须给这个属性。
"""

import asyncio
import itertools
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from langchain.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from plan_exec_fakes import RoutingStructuredFake, fake_app_config

from app.gateway.agents import AgentFactory
from app.gateway.routers import chat
from app.gateway.run_registry import RunRegistry
from evaharness.agents.lead_agent import LEAD_LANE
from evaharness.agents.options import RunOptions
from evaharness.agents.plan_exec.schemas import Plan, PlanTask, ReplanDecision
from evaharness.journal.stores import MemoryJournalStore

THREAD = "th-1"


@pytest.fixture(autouse=True)
def _sandbox_root(tmp_path, monkeypatch):
    """路由现在会建会话的文件空间——把它指到 ``tmp_path``，别往真的 ``backend/.evaai`` 里写。"""
    monkeypatch.setattr("app.tools.sandbox.SANDBOX_ROOT", tmp_path)
    return tmp_path


def _parse(frame: str):
    event = None
    data = None
    for line in frame.strip("\n").split("\n"):
        if line.startswith("event: "):
            event = line[len("event: ") :]
        elif line.startswith("data: "):
            data = json.loads(line[len("data: ") :])
    return event, data


def _fake_model(name: str | None = None, options: Any = None) -> BaseChatModel:  # noqa: ARG001
    """一个模型同时服务两条路径：主 Agent 要文本，planner / replanner 要结构化输出。

    第二个参数是运行参数（思考档位等）——这一层不关心它，但签名要接得住：路由会把每次
    请求的档位一路传下来。
    """
    del name, options
    return RoutingStructuredFake(
        messages=itertools.cycle([AIMessage(content="现在是 18:04")]),
        plan=Plan(tasks=[PlanTask(id="t1", description="查时间")]),
        decision=ReplanDecision(should_continue=True, reason="继续"),
    )


class _RecordingFactory(AgentFactory):
    """真工厂 + 记名册：让「每请求都建了新图」这条断言写得出来。

    不内省编译产物、不 patch 建图函数——只是在真路径上多记一笔，所以红/绿都指向产品代码。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.leads: list = []
        self.plans: list = []
        self.lead_options: list = []
        self.plan_options: list = []

    def lead(self, options=None):  # noqa: ANN001, ANN201
        graph = super().lead(options)
        self.leads.append(graph)
        self.lead_options.append(options)
        return graph

    def plan_exec(self, options=None):  # noqa: ANN001, ANN201
        roles = super().plan_exec(options)
        self.plans.append(roles)
        self.plan_options.append(options)
        return roles


def _client(store: MemoryJournalStore, journal_config=None) -> httpx.AsyncClient:
    """把 chat 路由挂在一个状态齐备的裸 app 上（不起 lifespan、不读配置文件）。

    checkpointer 在这里**显式共享**一个 ``InMemorySaver``：记忆本来就该跨请求活着，
    而图是每请求新的一张。工厂挂在 ``client.factory`` 上供断言取用。
    """
    factory = _RecordingFactory(fake_app_config(), [], checkpointer=InMemorySaver(), model_factory=_fake_model)
    app = FastAPI()
    app.state.journal_store = store
    app.state.journal_config = journal_config
    app.state.agent_factory = factory
    registry = RunRegistry()
    app.state.run_registry = registry
    app.include_router(chat.router)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    client.factory = factory  # type: ignore[attr-defined]
    client.registry = registry  # type: ignore[attr-defined]
    return client


class _JournalConfig:
    flush_threshold = 20


async def _post(client: httpx.AsyncClient, path: str, content: str, **extra):
    resp = await client.post(path, json={"content": content, "thread_id": THREAD, **extra})
    assert resp.status_code == 200, resp.text
    return [_parse(frame) for frame in resp.text.split("\n\n") if frame.strip()]


# --------------------------------------------------------------------------- #
# 默认路径：主 Agent
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_default_route_runs_the_lead_agent():
    """``/chat/stream`` 必须是主 Agent——它是「默认 ReAct 直答」这句话的唯一落点。"""
    async with _client(MemoryJournalStore(), _JournalConfig()) as client:
        frames = await _post(client, "/chat/stream", "现在几点？")

    names = [name for name, _ in frames]
    assert names[0] == "run_start"
    assert names[-1] == "end"
    assert "plan" not in names, "主 Agent 不该产出计划"
    assert "task_start" not in names
    assert "run_end" in names


@pytest.mark.asyncio
async def test_the_default_route_declares_react_mode():
    async with _client(MemoryJournalStore(), _JournalConfig()) as client:
        frames = await _post(client, "/chat/stream", "现在几点？")

    _, data = frames[0]
    assert data["mode"] == "react"
    assert data["thread_id"] == THREAD


@pytest.mark.parametrize("level", ["off", "low", "high", "max"])
@pytest.mark.asyncio
async def test_the_request_thinking_reaches_the_model_builder(level):
    """请求体里选的档位必须走到**建模型**那一步——否则界面上选了也是白选。"""
    async with _client(MemoryJournalStore(), _JournalConfig()) as client:
        frames = await _post(client, "/chat/stream", "现在几点？", thinking=level)

    assert client.factory.lead_options == [RunOptions(thinking=level)]
    assert frames[0][1]["thinking"] == level, "事件回显的要与请求一致，否则回放与实时分家"


@pytest.mark.asyncio
async def test_thinking_defaults_to_high_when_the_request_omits_it():
    """面向用户的默认档是 ``high``（与 ``RunStartEvent`` 那个历史回退的 ``off`` 是两回事）。"""
    async with _client(MemoryJournalStore(), _JournalConfig()) as client:
        frames = await _post(client, "/chat/stream", "现在几点？")

    assert client.factory.lead_options == [RunOptions(thinking="high")]
    assert frames[0][1]["thinking"] == "high"


@pytest.mark.asyncio
async def test_an_unknown_thinking_level_is_rejected():
    async with _client(MemoryJournalStore(), _JournalConfig()) as client:
        resp = await client.post("/chat/stream", json={"content": "你好", "thinking": "turbo"})

    assert resp.status_code == 422, "不认的档位该在进模型之前就被挡掉"


@pytest.mark.asyncio
async def test_the_default_route_uses_a_non_empty_lane():
    """空泳道 id 会让前端静默丢弃全部正文（``ensureBubble`` 对空值 no-op）。"""
    async with _client(MemoryJournalStore(), _JournalConfig()) as client:
        frames = await _post(client, "/chat/stream", "现在几点？")

    lanes = {data[1]["task_id"] for name, data in frames if name == "messages"}
    assert lanes == {LEAD_LANE}


@pytest.mark.asyncio
async def test_the_default_route_records_the_run():
    store = MemoryJournalStore()
    async with _client(store, _JournalConfig()) as client:
        await _post(client, "/chat/stream", "现在几点？")

    types = [entry.event.type for entry in await store.load(THREAD)]
    assert types[0] == "run_start"
    assert types[-1] == "run_end"


# --------------------------------------------------------------------------- #
# 封存路径：Plan-Exec
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_plan_route_still_runs_plan_exec():
    """封存不等于坏掉：路由还在，且仍然产出它那套事件。"""
    async with _client(MemoryJournalStore(), _JournalConfig()) as client:
        frames = await _post(client, "/chat/plan/stream", "现在几点？")

    names = [name for name, _ in frames]
    assert names[0] == "run_start"
    assert "plan" in names
    assert "task_start" in names
    assert "summary" in names
    assert names[-1] == "end"


@pytest.mark.asyncio
async def test_the_plan_route_declares_plan_mode():
    async with _client(MemoryJournalStore(), _JournalConfig()) as client:
        frames = await _post(client, "/chat/plan/stream", "现在几点？")

    _, data = frames[0]
    assert data["mode"] == "plan"


# --------------------------------------------------------------------------- #
# 参数校验（两条路径共用一套外壳）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_illegal_thread_id_is_rejected_before_the_run():
    """非法 id 会是 journal 的路径段，必须在入口挡成 400 而不是写盘时炸成 500。"""
    async with _client(MemoryJournalStore(), _JournalConfig()) as client:
        for path in ("/chat/stream", "/chat/plan/stream"):
            resp = await client.post(path, json={"content": "你好", "thread_id": "坏.id"})
            assert resp.status_code == 400, path


@pytest.mark.asyncio
async def test_a_generated_thread_id_comes_back_in_run_start():
    """不给 id 时服务端生成，并随首个事件回传——前端因此不必先建会话。"""
    async with _client(MemoryJournalStore(), _JournalConfig()) as client:
        resp = await client.post("/chat/stream", json={"content": "你好"})

    _, data = _parse(resp.text.split("\n\n")[0])
    assert data["thread_id"]


# --------------------------------------------------------------------------- #
# 每请求建图（T9）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_first_message_creates_the_session_file_space(_sandbox_root):
    """会话一诞生就有它的文件空间——用户要能在对话前把文件放进 /input。

    这条也顺带钉住「不依赖模型去调工具」：这次运行里一个文件工具都没被调用。
    """
    async with _client(MemoryJournalStore(), _JournalConfig()) as client:
        await _post(client, "/chat/stream", "你好")

    for name in ("input", "output", "workspace"):
        assert (_sandbox_root / "user" / THREAD / name).is_dir(), name


@pytest.mark.asyncio
async def test_the_plan_route_creates_it_too(_sandbox_root):
    async with _client(MemoryJournalStore(), _JournalConfig()) as client:
        await _post(client, "/chat/plan/stream", "你好")

    assert (_sandbox_root / "user" / THREAD / "workspace").is_dir()


@pytest.mark.asyncio
async def test_an_illegal_thread_id_never_creates_anything(_sandbox_root):
    """400 必须发生在建目录**之前**——否则一个畸形 id 也能在磁盘上留痕。"""
    async with _client(MemoryJournalStore(), _JournalConfig()) as client:
        resp = await client.post("/chat/stream", json={"content": "你好", "thread_id": "坏.id"})

    assert resp.status_code == 400
    assert not (_sandbox_root / "user").exists()


@pytest.mark.asyncio
async def test_each_request_builds_its_own_lead_graph():
    """**这条断言在 T9 之前根本写不出来**：那时 ``app.state`` 上挂的是图本身。

    它守的是并发洞被消除的充分条件——两次请求拿到两张图，中间件实例自然是两套；
    共享只会在「路由复用同一个编译产物」时重新出现（也是「不做缓存」那条决策的回归）。
    """
    async with _client(MemoryJournalStore(), _JournalConfig()) as client:
        await _post(client, "/chat/stream", "现在几点？")
        await _post(client, "/chat/stream", "现在几点？")

    leads = client.factory.leads
    assert len(leads) == 2, "两个请求应当各建一张图"
    assert leads[0] is not leads[1]


@pytest.mark.asyncio
async def test_each_request_builds_its_own_plan_roles():
    """封存路径同样是每请求一份，而且三件套一起换。"""
    async with _client(MemoryJournalStore(), _JournalConfig()) as client:
        await _post(client, "/chat/plan/stream", "现在几点？")
        await _post(client, "/chat/plan/stream", "现在几点？")

    plans = client.factory.plans
    assert len(plans) == 2
    assert plans[0].planner is not plans[1].planner
    assert plans[0].executor is not plans[1].executor
    assert plans[0].replanner is not plans[1].replanner


# --------------------------------------------------------------------------- #
# 主动取消端点（T5）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cancel_returns_404_when_nothing_is_running():
    async with _client(MemoryJournalStore(), _JournalConfig()) as client:
        resp = await client.post("/chat/cancel", json={"run_id": "nope"})

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cancel_cancels_a_registered_run():
    async with _client(MemoryJournalStore(), _JournalConfig()) as client:
        task = asyncio.create_task(asyncio.Event().wait())  # 永不结束的占位
        client.registry.register("r1", task)

        resp = await client.post("/chat/cancel", json={"run_id": "r1"})

    assert resp.status_code == 200
    assert resp.json() == {"cancelled": True}
    with pytest.raises(asyncio.CancelledError):
        await task
