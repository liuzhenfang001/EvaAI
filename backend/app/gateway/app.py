"""EvaAI gateway：FastAPI 入口 + 内嵌 agent 装配 + SSE 流式。

默认路径是 **ReAct 主 Agent**（``/chat/stream``）；Plan-Exec 退到 ``/chat/plan/stream``
封存（见 ADR-0010）。

启动：``uv run uvicorn app.gateway.app:app --port 8010``
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langchain.chat_models import BaseChatModel

from app.gateway.routers import chat, threads
from app.prompts import EXECUTOR_PROMPT, PLANNER_PROMPT, REPLANNER_PROMPT, SUBAGENT_PROMPT, SYSTEM_PROMPT
from app.tools.builtin import get_current_time
from evaharness.agents.lead_agent import (
    build_lead_middlewares,
    make_lead_agent,
    make_subagent_graph,
    make_task_tool,
)
from evaharness.agents.plan_exec import make_planner, make_react_executor, make_replanner
from evaharness.checkpoint.sqlite_saver import make_sqlite_checkpointer, prune_checkpoints
from evaharness.config.app_config import AppConfig, JournalConfig
from evaharness.config.loader import load_config, load_env_file
from evaharness.journal.factory import make_journal_store
from evaharness.models.factory import create_chat_model

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
    """启动时加载三层配置、构建模型与全部 agent，挂到 app.state。

    图在装配期各建**一次**并被所有请求共享：lead agent 靠会话级 checkpoint 坐标隔离，
    子 agent 靠运行级坐标，Plan-Exec 的任务靠 ``{会话}:{运行}:{任务}``。
    """
    load_env_file(PROJECT_ROOT / ".env")
    config_data = load_config(PROJECT_ROOT / "config.yaml")
    app_config = AppConfig.model_validate(config_data)
    model = create_chat_model(app_config)

    # AsyncSqliteSaver 需要活跃的事件循环，所以只能在这里构造；由 ExitStack 持有到关机。
    async with AsyncExitStack() as stack:
        checkpoint_path = _resolve_path(app_config.checkpoint.db_path)
        saver = await stack.enter_async_context(make_sqlite_checkpointer(checkpoint_path))
        # 挂到 state：删会话时要按前缀连带清掉它的任务级 checkpoint。
        app.state.checkpointer = saver
        await _prune_checkpoints(saver, app_config.checkpoint.retention_days)

        journal_config = _resolve_journal_config(app_config.journal)
        app.state.journal_config = journal_config
        app.state.journal_store = make_journal_store(journal_config)

        # 子 agent 先建：主 Agent 的 `task` 工具要闭包住它。
        app.state.subagent = make_subagent_graph(
            model,
            BUSINESS_TOOLS,
            system_prompt=SUBAGENT_PROMPT,
            checkpointer=saver,
        )
        app.state.lead_agent = make_lead_agent(
            model,
            [*BUSINESS_TOOLS, make_task_tool(app.state.subagent)],
            system_prompt=SYSTEM_PROMPT,
            middleware=build_lead_middlewares(
                model,
                summarization=app_config.agent.summarization,
                summary_model=_summary_model(app_config, model),
            ),
            checkpointer=saver,
        )

        # Plan-Exec：封存路径，路由仍在（/chat/plan/stream），没有 UI 入口。
        app.state.planner = make_planner(model, system_prompt=PLANNER_PROMPT)
        app.state.executor = make_react_executor(
            model,
            BUSINESS_TOOLS,
            system_prompt=EXECUTOR_PROMPT,
            checkpointer=saver,
        )
        app.state.replanner = make_replanner(model, system_prompt=REPLANNER_PROMPT)
        yield


def _summary_model(app_config: AppConfig, default: BaseChatModel) -> BaseChatModel:
    """摘要用哪个模型：配置里指定了 profile 就单独建一个，否则复用主模型。"""
    name = app_config.agent.summarization.model
    return create_chat_model(app_config, name) if name else default


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
