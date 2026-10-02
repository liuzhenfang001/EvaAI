"""lifespan 的形状：只把**长命物**挂到 ``app.state``（T9）。

此前没有任何测试跑 lifespan，所以「``app.state`` 只留长命物」这条完全没有护栏——
而它正是 T9 的一半内容。这里用 ``SimpleNamespace`` 冒充 app（lifespan 只碰 ``app.state``），
把配置、checkpointer、journal store 与模型全换成假的，再断言：建图工厂在，**图与模型
一个都不在**。
"""

import itertools
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from plan_exec_fakes import ToolCapableFake

from app.gateway.agents import AgentFactory
from app.gateway.app import lifespan

GHOSTS = ("lead_agent", "subagent", "planner", "executor", "replanner", "model")
"""T9 之前挂在 ``app.state`` 上的东西。它们不该回来。"""


@pytest.mark.asyncio
async def test_lifespan_attaches_only_long_lived_state(monkeypatch):
    built: list[str | None] = []

    @asynccontextmanager
    async def fake_saver(_path):
        yield InMemorySaver()

    async def no_prune(_saver, _retention_days):  # noqa: ARG001
        return None

    def fake_model(_config, name=None, **_kwargs):  # noqa: ANN001, ANN003
        built.append(name)
        return ToolCapableFake(messages=itertools.cycle([AIMessage(content="好的。")]))

    monkeypatch.setattr("app.gateway.app.load_env_file", lambda _path: None)
    monkeypatch.setattr(
        "app.gateway.app.load_config",
        lambda _path: {"config_version": 1, "models": [{"name": "fake", "model": "fake"}]},
    )
    monkeypatch.setattr("app.gateway.app.make_sqlite_checkpointer", fake_saver)
    monkeypatch.setattr("app.gateway.app._prune_checkpoints", no_prune)
    monkeypatch.setattr("app.gateway.app.make_journal_store", lambda _config: object())
    monkeypatch.setattr("app.gateway.agents.create_chat_model", fake_model)

    app = SimpleNamespace(state=SimpleNamespace())

    async with lifespan(app):
        assert isinstance(app.state.agent_factory, AgentFactory), "建图的唯一入口不见了"
        assert isinstance(app.state.checkpointer, InMemorySaver), "checkpointer 必须长命"
        assert app.state.app_config is not None
        for ghost in GHOSTS:
            assert not hasattr(app.state, ghost), f"app.state 上不该再有 {ghost}"

    assert built, "启动期 smoke build 没有建模型——装配错误会被拖到第一个请求才炸"


@pytest.mark.asyncio
async def test_a_broken_assembly_refuses_to_start(monkeypatch):
    """smoke build 失败要**拒绝启动**，不是记条日志继续。

    默认路径都建不起来，服务起来也没意义；而「起得来、每个请求 500」是比崩溃更难查的
    失败模式——前端期望 SSE，拿到 JSON 500 时界面上只是「没有任何反应」。
    """

    @asynccontextmanager
    async def fake_saver(_path):
        yield InMemorySaver()

    async def no_prune(_saver, _retention_days):  # noqa: ARG001
        return None

    def broken_model(_config, _name=None, **_kwargs):  # noqa: ANN001, ANN003
        raise RuntimeError("模型 profile 的 use 指向了不存在的类")

    monkeypatch.setattr("app.gateway.app.load_env_file", lambda _path: None)
    monkeypatch.setattr(
        "app.gateway.app.load_config",
        lambda _path: {"config_version": 1, "models": [{"name": "fake", "model": "fake"}]},
    )
    monkeypatch.setattr("app.gateway.app.make_sqlite_checkpointer", fake_saver)
    monkeypatch.setattr("app.gateway.app._prune_checkpoints", no_prune)
    monkeypatch.setattr("app.gateway.app.make_journal_store", lambda _config: object())
    monkeypatch.setattr("app.gateway.agents.create_chat_model", broken_model)

    with pytest.raises(RuntimeError, match="不存在的类"):
        async with lifespan(SimpleNamespace(state=SimpleNamespace())):
            pass
