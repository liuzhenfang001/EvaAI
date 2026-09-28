"""EvaAI gateway：FastAPI 入口 + 内嵌 Plan-Exec 编排 + SSE 流式。

启动：``uv run uvicorn app.gateway.app:app --port 8001``
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.gateway.routers import chat
from app.prompts import EXECUTOR_PROMPT, PLANNER_PROMPT, REPLANNER_PROMPT
from app.tools.builtin import get_current_time
from evaharness.agents.plan_exec import make_planner, make_react_executor, make_replanner
from evaharness.checkpoint.sqlite_saver import make_sqlite_checkpointer, prune_checkpoints
from evaharness.config.app_config import AppConfig
from evaharness.config.loader import load_config, load_env_file
from evaharness.models.factory import create_chat_model

# backend/app/gateway/app.py -> 项目根（config.yaml / .env 所在目录）
PROJECT_ROOT = Path(__file__).resolve().parents[3]

TOOLS = [get_current_time]

logger = logging.getLogger(__name__)


def _resolve_db_path(db_path: str) -> Path:
    """配置里的相对路径按项目根解析，避免受启动目录影响。"""
    path = Path(db_path)
    return path if path.is_absolute() else PROJECT_ROOT / path


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时加载三层配置、构建模型与 Plan-Exec 三件套，挂到 app.state。"""
    load_env_file(PROJECT_ROOT / ".env")
    config_data = load_config(PROJECT_ROOT / "config.yaml")
    app_config = AppConfig.model_validate(config_data)
    model = create_chat_model(app_config)

    # AsyncSqliteSaver 需要活跃的事件循环，所以只能在这里构造；由 ExitStack 持有到关机。
    async with AsyncExitStack() as stack:
        checkpoint_path = _resolve_db_path(app_config.checkpoint.db_path)
        saver = await stack.enter_async_context(make_sqlite_checkpointer(checkpoint_path))
        await _prune_checkpoints(saver, app_config.checkpoint.retention_days)

        app.state.planner = make_planner(model, system_prompt=PLANNER_PROMPT)
        app.state.executor = make_react_executor(
            model,
            TOOLS,
            system_prompt=EXECUTOR_PROMPT,
            checkpointer=saver,
        )
        app.state.replanner = make_replanner(model, system_prompt=REPLANNER_PROMPT)
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
    return app


app = create_app()
