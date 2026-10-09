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
from app.gateway.mcp import load_mcp_tools
from app.gateway.routers import chat, files, threads
from app.gateway.run_registry import RunRegistry
from app.gateway.skills import project_skills
from app.tools.registry import build_tools
from app.tools.sandbox import skills_view_root
from evaharness.checkpoint.sqlite_saver import make_sqlite_checkpointer, prune_checkpoints
from evaharness.config.app_config import AppConfig, JournalConfig
from evaharness.config.extensions_config import ExtensionsConfig
from evaharness.config.loader import load_config, load_env_file, load_extensions
from evaharness.journal.factory import make_journal_store

# backend/app/gateway/app.py -> 项目根（config.yaml / .env 所在目录）
PROJECT_ROOT = Path(__file__).resolve().parents[3]

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

    # 第三层配置（T13 第一次给它接上消费者）：MCP server 的声明。
    #
    # 它和 .env / config.yaml 一样是**必须存在**的一层——文件缺失就拒绝启动，与另两层同口径
    # （所以 README 的「快速开始」里有一句 cp）。一个写错的 server 不会拖垮整份文件：
    # 坏的在那里就被摘掉并记警告（见 `parse_mcp_servers`）。
    extensions = ExtensionsConfig.model_validate(
        load_extensions(PROJECT_ROOT / "extensions_config.json")
    )
    app.state.extensions_config = extensions

    # 工具从配置里来（注册项写错、重名、name 不匹配、工厂参数写错都在这里炸）。必须在
    # smoke build 之前——smoke build 正是要拿这份列表去试装配。
    business_tools = build_tools(app_config)

    # 技能（T14）：把仓库里的源目录**镜像**到 `/skills` 的只读视图，再把清单交给建图工厂去拼进提示词。
    #
    # 放在**启动期**、`build_tools` 之后：技能是可选内容，坏了只记警告不拦启动（决议 #5），所以它
    # 与「上面的配置写错就拒绝启动」是两种性格；放在这里是因为它确定、快，且不需要事件循环。
    # 源在仓库里（进版本控制），视图在 `.evaai/` 下（运行期数据）——两层分工见 ADR-0018。
    skills = project_skills(_resolve_path("skills"), skills_view_root())
    app.state.skills = skills

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
        # MCP：**连一次、把工具列表留下来**（之后就再不连了——运行期零 I/O，连接推迟到模型真的调
        # 那个工具时，见 `app.gateway.mcp`）。它与技能投影同属「外部内容」，所以失败也只警告不拦
        # 启动；与 smoke build 的分工是「我们自己的装配错→拒绝启动，外部依赖不可用→只警告」。
        #
        # 放在 AgentFactory 之前：工具表是构造参数，MCP 工具与业务工具在建图眼里没有区别。
        mcp_tools = await load_mcp_tools(extensions.mcp_servers, business_tools=business_tools)
        app.state.mcp_tools = mcp_tools
        all_tools = [*business_tools, *mcp_tools]

        app.state.agent_factory = AgentFactory(
            app_config, all_tools, checkpointer=saver, skills=skills
        )

        # 运行中 run 的登记表：取消端点靠它触达正在跑的 run（T5）。与 checkpointer 一样
        # 是长命共享物，但**没有活跃事件循环的约束**——只是一个 dict。
        app.state.run_registry = RunRegistry()

        # 启动期 smoke build：装配一遍再丢掉，把「启动期就该发现」的错误挡在服务起来之前
        # ——模型 profile 的 use 指向不存在的类、中间件链里塞了非中间件对象、工具列表里塞了
        # 字符串、api key / base_url 缺失。失败就**拒绝启动**，不降级成 warning。
        #
        # **它不是正确性证明。** 建图期抓不到的至少两类（实测）：(1) 工具重名——
        # create_agent 静默接受（这条已由 registry.build_tools 在**更早**的地方拦掉，见 ADR-0012）；
        # (2) state_schema / context_schema 传错——同样不报错，换错要等 task 工具派发那一刻才炸。
        # 它抓的是「装配连线断了」，不是「语义接错了」。
        #
        # checkpointer=None 是有意的：这一步只验装配，不该和 saver 的生命周期绑在一起。
        smoke = AgentFactory(app_config, all_tools, checkpointer=None, skills=skills)
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
    # 文件卡片点「下载」走的就是它（GET /threads/{会话}/files/{路径}）。
    app.include_router(files.router)
    return app


app = create_app()
