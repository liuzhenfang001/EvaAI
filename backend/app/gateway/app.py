"""EvaAI gateway：FastAPI 入口 + 内嵌 ReAct agent + SSE 流式。

启动：``uv run uvicorn app.gateway.app:app --port 8001``
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.gateway.routers import chat
from app.tools.builtin import get_current_time
from evaharness.agents.react import make_react_agent
from evaharness.config.app_config import AppConfig
from evaharness.config.loader import load_config, load_env_file
from evaharness.models.factory import create_chat_model

# backend/app/gateway/app.py -> 项目根（config.yaml / .env 所在目录）
PROJECT_ROOT = Path(__file__).resolve().parents[3]

SYSTEM_PROMPT = "你是 EvaAI，一个本地部署的个人 AI 助手。请用中文回答用户，简洁准确。"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时加载三层配置、构建模型与 ReAct agent，挂到 app.state。"""
    load_env_file(PROJECT_ROOT / ".env")
    config_data = load_config(PROJECT_ROOT / "config.yaml")
    app_config = AppConfig.model_validate(config_data)
    model = create_chat_model(app_config)
    app.state.agent = make_react_agent(model, [get_current_time], system_prompt=SYSTEM_PROMPT)
    yield


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
