"""lifespan 的形状：只把**长命物**挂到 ``app.state``（T9），并且工具在启动期就建好（T4）。

此前没有任何测试跑 lifespan，所以「``app.state`` 只留长命物」这条完全没有护栏——
而它正是 T9 的一半内容。这里用 ``SimpleNamespace`` 冒充 app（lifespan 只碰 ``app.state``），
把配置、checkpointer、journal store 与模型全换成假的，再断言：建图工厂在，**图与模型
一个都不在**。
"""

import itertools
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fakes import ToolCapableFake
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.gateway.agents import AgentFactory
from app.gateway.app import lifespan
from evaharness.sandbox import SandboxLayout

GHOSTS = ("lead_agent", "subagent", "planner", "executor", "replanner", "model")
"""T9 之前挂在 ``app.state`` 上的东西。它们不该回来。"""

# 最小的工具注册表：只留一个不需要任何密钥的纯函数工具，避免测试依赖 .env。
_ONE_TOOL = [{"name": "get_current_time", "use": "app.tools.builtin:get_current_time"}]


def _config(**overrides: object) -> dict:
    return {
        "config_version": 1,
        "models": [{"name": "fake", "model": "fake"}],
        "tools": _ONE_TOOL,
        **overrides,
    }


@pytest.fixture(autouse=True)
def _extensions(monkeypatch):
    """第三层配置指向一份**固定的空文件**，不去读仓库里那份真文件。

    T13 起 lifespan 会读它，而它从此是要被 gitignore 的（模板入库、真文件忽略）——测试依赖它
    存在，就等于依赖「开发者本机跑过 `cp`」。空 `mcpServers` 也让启动期探测无事可做。
    """
    monkeypatch.setattr("app.gateway.app.load_extensions", lambda _path: {"mcpServers": {}})


@pytest.fixture(autouse=True)
def _skills(monkeypatch):
    """技能投影换成空实现，**不碰真的 `backend/.evaai/skills-view`**。

    T14 起 lifespan 会把仓库里的 `skills/` 镜像到运行期目录——那是**真实磁盘写入**。测试既不该依赖
    它，也不该污染它（今天仓库里只有示例技能，看不出问题；等哪天技能写坏了或有人在跑测试的同时改技能，
    症状会非常难查）。投影自身的契约在 `test_skills_projection.py`。
    """
    monkeypatch.setattr("app.gateway.app.project_skills", lambda _src, _view: [])


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
    monkeypatch.setattr("app.gateway.app.load_config", lambda _path: _config())
    monkeypatch.setattr("app.gateway.app.make_sqlite_checkpointer", fake_saver)
    monkeypatch.setattr("app.gateway.app._prune_checkpoints", no_prune)
    monkeypatch.setattr("app.gateway.app.make_journal_store", lambda _config: object())
    monkeypatch.setattr("app.gateway.agents.create_chat_model", fake_model)

    app = SimpleNamespace(state=SimpleNamespace())

    async with lifespan(app):
        assert isinstance(app.state.agent_factory, AgentFactory), "建图的唯一入口不见了"
        assert isinstance(app.state.checkpointer, InMemorySaver), "checkpointer 必须长命"
        assert app.state.app_config is not None
        # 第三层配置也是长命物：**MCP 工具在启动期装载一次**，之后每次运行都用这份清单。
        assert app.state.extensions_config is not None
        assert app.state.mcp_tools == [], "没配 server 时该是空表（`_extensions` fixture 给的就是空）"
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
    monkeypatch.setattr("app.gateway.app.load_config", lambda _path: _config())
    monkeypatch.setattr("app.gateway.app.make_sqlite_checkpointer", fake_saver)
    monkeypatch.setattr("app.gateway.app._prune_checkpoints", no_prune)
    monkeypatch.setattr("app.gateway.app.make_journal_store", lambda _config: object())
    monkeypatch.setattr("app.gateway.agents.create_chat_model", broken_model)

    with pytest.raises(RuntimeError, match="不存在的类"):
        async with lifespan(SimpleNamespace(state=SimpleNamespace())):
            pass


@pytest.mark.asyncio
async def test_a_broken_tool_entry_refuses_to_start(monkeypatch):
    """工具注册项写错也要**拒绝启动**——这是 T4 把「建图期抓不到重名」那条盲区补上的地方。

    如果拖到请求期才发现，失败会藏在一条 SSE 流的中间，比启动崩溃难查一个量级。
    """
    monkeypatch.setattr("app.gateway.app.load_env_file", lambda _path: None)
    monkeypatch.setattr(
        "app.gateway.app.load_config",
        lambda _path: _config(tools=[{"name": "nope", "use": "app.tools.no_such_module:tool"}]),
    )

    with pytest.raises(Exception, match="no_such_module"):
        async with lifespan(SimpleNamespace(state=SimpleNamespace())):
            pass


# --------------------------------------------------------------------------- #
# docker 沙箱（T15）
# --------------------------------------------------------------------------- #

_BASH_TOOL = {"name": "bash", "use": "evaharness.sandbox.bash:make_bash", "image": "img:1"}


class _Store:
    """只实现 lifespan 真正用到的那一个方法。"""

    def __init__(self, *sessions: str) -> None:
        self._sessions = [SimpleNamespace(thread_id=session) for session in sessions]

    async def threads(self):
        return self._sessions


def _stub_lifespan(monkeypatch, tmp_path, *, tools=None, store=None) -> None:
    """把 lifespan 的外部依赖全换成假的（上面几条测试各自铺的那一套，这里抽出来复用）。"""

    @asynccontextmanager
    async def fake_saver(_path):
        yield InMemorySaver()

    async def no_prune(_saver, _retention_days):  # noqa: ARG001
        return None

    def fake_model(_config, name=None, **_kwargs):  # noqa: ANN001, ANN003
        return ToolCapableFake(messages=itertools.cycle([AIMessage(content="好的。")]))

    # 布局指向 tmp_path：启用了 bash 的用例会在 lifespan 里建哨兵文件，别写真的 backend/.evaai。
    monkeypatch.setattr("app.gateway.app.build_layout", lambda: SandboxLayout(root=Path(tmp_path)))
    monkeypatch.setattr("app.gateway.app.load_env_file", lambda _path: None)
    monkeypatch.setattr("app.gateway.app.load_config", lambda _path: _config(tools=tools or _ONE_TOOL))
    monkeypatch.setattr("app.gateway.app.make_sqlite_checkpointer", fake_saver)
    monkeypatch.setattr("app.gateway.app._prune_checkpoints", no_prune)
    monkeypatch.setattr("app.gateway.app.make_journal_store", lambda _config: store or _Store())
    monkeypatch.setattr("app.gateway.agents.create_chat_model", fake_model)


@pytest.mark.asyncio
async def test_bash_that_cannot_start_refuses_to_boot(monkeypatch, tmp_path):
    """启用了 bash 却用不了 = 坏部署：早说。拖到模型第一次调用才炸，离「你刚改的配置」就很远了。"""
    from evaharness.sandbox import DockerError

    def refuse(_image: str) -> None:
        raise DockerError("docker daemon 连不上，bash 工具没法用")

    _stub_lifespan(monkeypatch, tmp_path, tools=[*_ONE_TOOL, _BASH_TOOL])
    monkeypatch.setattr("evaharness.sandbox.bash.check_docker_ready", refuse)

    with pytest.raises(DockerError, match="daemon 连不上"):
        async with lifespan(SimpleNamespace(state=SimpleNamespace())):
            pass


@pytest.mark.asyncio
async def test_bash_enabled_reconciles_orphan_containers_at_startup(monkeypatch, tmp_path):
    """启动期把孤儿容器清掉——清理失败只警告，**不拦启动**（与 checkpoint 清理同口径）。"""
    seen: dict = {}

    def fake_reconcile(layout, live, **_kwargs):  # noqa: ANN001, ANN003
        seen["layout"] = layout
        seen["live"] = live
        return 2

    _stub_lifespan(monkeypatch, tmp_path, tools=[*_ONE_TOOL, _BASH_TOOL], store=_Store("th-alive"))
    monkeypatch.setattr("evaharness.sandbox.bash.check_docker_ready", lambda image: None)
    monkeypatch.setattr("app.gateway.app.docker.reconcile_orphans", fake_reconcile)

    app = SimpleNamespace(state=SimpleNamespace())
    async with lifespan(app):
        assert app.state.bash_enabled is True, "DELETE 会话那条路由靠它决定要不要收尾"

    assert seen["live"] == {"th-alive"}, "判据是 journal 里的会话，不是磁盘目录"


@pytest.mark.asyncio
async def test_a_failing_cleanup_does_not_block_startup(monkeypatch, tmp_path):
    def boom(layout, live, **_kwargs):  # noqa: ANN001, ARG001
        raise RuntimeError("docker 不见了")

    _stub_lifespan(monkeypatch, tmp_path, tools=[*_ONE_TOOL, _BASH_TOOL])
    monkeypatch.setattr("evaharness.sandbox.bash.check_docker_ready", lambda image: None)
    monkeypatch.setattr("app.gateway.app.docker.reconcile_orphans", boom)

    app = SimpleNamespace(state=SimpleNamespace())
    async with lifespan(app):
        assert app.state.bash_enabled is True


@pytest.mark.asyncio
async def test_without_a_bash_entry_docker_is_never_touched(monkeypatch, tmp_path):
    """没启用 bash 的部署（模板的默认状态）一个 docker 命令都不该发。"""

    def boom(*_args, **_kwargs):
        raise AssertionError("没启用 bash 却碰了 docker")

    _stub_lifespan(monkeypatch, tmp_path)
    monkeypatch.setattr("app.gateway.app.docker.reconcile_orphans", boom)

    app = SimpleNamespace(state=SimpleNamespace())
    async with lifespan(app):
        assert app.state.bash_enabled is False
