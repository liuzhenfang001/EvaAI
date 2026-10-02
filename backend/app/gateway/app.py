"""EvaAI gateway：FastAPI 入口 + SSE 流式。

默认路径是 **ReAct 主 Agent**（``/chat/stream``）；Plan-Exec 退到 ``/chat/plan/stream``
封存（见 ADR-0010）。图与模型**不在这里建**——由
:class:`~app.gateway.agents.AgentFactory` 在每个请求里现建（见 ADR-0011）。

启动：``uv run uvicorn app.gateway.app:app --port 8010``
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.gateway.agents import AgentFactory
from app.gateway.routers import chat, threads
from app.tools.builtin import get_current_time
from evaharness.checkpoint.sqlite_saver import make_sqlite_checkpointer, prune_checkpoints
from evaharness.config.app_config import AppConfig, JournalConfig
from evaharness.config.loader import load_config, load_env_file
from evaharness.journal.factory import make_journal_store

# backend/app/gateway/app.py -> 项目根（config.yaml / .env 所在目录）
PROJECT_ROOT = Path(__file__).resolve().parents[3]

BUSINESS_TOOLS = [get_current_time]
"""业务工具的**唯一**定义处。

lead agent 与子 agent 各自拿一份：主 Agent 多一个 ``task`` 工具，子 agent 只有业务工具
（这是防递归的结构保证，见 ``make_subagent_graph``）。
"""

logger = logging.getLogger(__name__)


def _resolve_path(path: str) -> Path:
    """配置里的相对路径按项目根解析，避免受启动目录影响。"""
    resolved = Path(path)
    return resolved if resolved.is_absolute() else PROJECT_ROOT / resolved


def _resolve_journal_config(config: JournalConfig) -> JournalConfig:
    """把 journal 的落盘目录锚到项目根（它来自配置，而不是 store 的默认值）。"""
    if config.dir is None:
        return config
    return config.model_copy(update={"dir": str(_resolve_path(config.dir))})


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时加载三层配置，把**长命物**挂到 app.state；图与模型不在这里建。

    图与模型改由 :class:`~app.gateway.agents.AgentFactory` 在每个请求里现建（ADR-0011）：
    请求级的生命周期让中间件的追踪状态不再跨请求，同时打开「按请求配置」的门。这里只留
    建图所需的参数与真正长命的共享物。
    """
    load_env_file(PROJECT_ROOT / ".env")
    config_data = load_config(PROJECT_ROOT / "config.yaml")
    app_config = AppConfig.model_validate(config_data)
    # 下一片「每请求参数从哪来」要从这里长出去（请求体 / 按会话持久化 / 全局 + 覆盖）。
    app.state.app_config = app_config

    # AsyncSqliteSaver 需要活跃的事件循环，所以只能在这里构造；由 ExitStack 持有到关机。
    # 它也是**唯一**先天不可能每请求建的建图输入（ADR-0007）。
    async with AsyncExitStack() as stack:
        checkpoint_path = _resolve_path(app_config.checkpoint.db_path)
        saver = await stack.enter_async_context(make_sqlite_checkpointer(checkpoint_path))
        # 挂到 state：删会话时要按前缀连带清掉它的任务级 checkpoint。
        app.state.checkpointer = saver
        await _prune_checkpoints(saver, app_config.checkpoint.retention_days)

        journal_config = _resolve_journal_config(app_config.journal)
        app.state.journal_config = journal_config
        app.state.journal_store = make_journal_store(journal_config)

        # 建图的唯一入口。它只持有参数、不持有图：每个请求调 lead() / plan_exec() 现建。
        app.state.agent_factory = AgentFactory(app_config, BUSINESS_TOOLS, checkpointer=saver)

        # 启动期 smoke build：装配一遍再丢掉，把「启动期就该发现」的错误挡在服务起来之前
        # ——模型 profile 的 use 指向不存在的类、中间件链里塞了非中间件对象、工具列表里塞了
        # 字符串、api key / base_url 缺失。失败就**拒绝启动**，不降级成 warning。
        #
        # **它不是正确性证明。** 建图期抓不到的至少两类（实测）：(1) 工具重名——
        # create_agent 静默接受；(2) state_schema / context_schema 传错——同样不报错，
        # 换错要等 task 工具派发那一刻才炸。它抓的是「装配连线断了」，不是「语义接错了」。
        #
        # checkpointer=None 是有意的：这一步只验装配，不该和 saver 的生命周期绑在一起。
        smoke = AgentFactory(app_config, BUSINESS_TOOLS, checkpointer=None)
        smoke.lead()
        smoke.plan_exec()

        yield


async def _prune_checkpoints(saver, retention_days: int) -> None:
    """启动时清理过期 checkpoint。失败只记日志——清理不该拦着服务起来。"""
    try:
        deleted = await prune_checkpoints(saver, retention_days=retention_days)
    except Exception:
        logger.warning("Checkpoint 清理失败，跳过", exc_info=True)
        return
    if deleted:
        logger.info("已清理 %d 个过期 checkpoint thread", deleted)


def create_app() -> FastAPI:
    app = FastAPI(title="EvaAI", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        # 本地个人助手无登录/权限边界，CORS 放开所有来源，覆盖 localhost / 127.0.0.1 / LAN IP。
        # allow_private_network 处理 Chrome 的 Private Network Access 预检（LAN 页面请求 loopback）。
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_private_network=True,
    )
    app.include_router(chat.router)
    app.include_router(threads.router)
    return app


app = create_app()
