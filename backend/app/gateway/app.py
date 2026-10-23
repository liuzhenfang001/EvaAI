"""EvaAI gateway：FastAPI 入口 + SSE 流式。

只有一条路径：**ReAct 主 Agent**（``/chat/stream``）。Plan-Exec 在 T16 下线（ADR-0021）。
图与模型**不在这里建**——由
:class:`~app.gateway.agents.AgentFactory` 在每个请求里现建（见 ADR-0011）。

启动：``uv run uvicorn app.gateway.app:app --port 8010``
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.gateway.agents import AgentFactory
from app.gateway.mcp import load_mcp_tools
from app.gateway.routers import chat, files, memory, threads
from app.gateway.skills import project_skills
from app.tools.knowledge import start_knowledge
from app.tools.memory import make_extractor, start_memory
from app.tools.registry import build_tools
from app.tools.sandbox_layout import build_layout
from evaharness.checkpoint.sqlite_saver import make_sqlite_checkpointer, prune_checkpoints
from evaharness.config.app_config import AppConfig, JournalConfig
from evaharness.config.extensions_config import ExtensionsConfig
from evaharness.config.loader import load_config, load_env_file, load_extensions
from evaharness.journal.factory import make_journal_store
from evaharness.logging import configure_logging
from evaharness.runtime import RunRegistry
from evaharness.sandbox import BASH_TOOL_NAME, docker, ensure_mount_check

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
    # 消费 `log_level`：给 `app` / `evaharness` 两个命名空间配级别与 handler。
    #
    # 放在这里、而不是模块导入期：级别来自 config.yaml，而 uvicorn 自己的日志配置**从不碰 root**，
    # 我们的 logger 因此默认继承 root 的 WARNING——INFO 全被丢掉、WARNING 只剩裸消息。不配的话，
    # 「知识库无变化，跳过重建」这类 INFO 在真实部署里一条都看不见（见 ADR-0023）。
    configure_logging(app_config.log_level)
    # 下一片「每请求参数从哪来」要从这里长出去（请求体 / 按会话持久化 / 全局 + 覆盖）。
    app.state.app_config = app_config

    # 第三层配置（T13 第一次给它接上消费者）：MCP server 的声明。
    #
    # 它和 .env / config.yaml 一样是**必须存在**的一层——文件缺失就拒绝启动，与另两层同口径
    # （所以 README 的「快速开始」里有一句 cp）。一个写错的 server 不会拖垮整份文件：
    # 坏的在那里就被摘掉并记警告（见 `parse_mcp_servers`）。
    extensions = ExtensionsConfig.model_validate(load_extensions(PROJECT_ROOT / "extensions_config.json"))
    app.state.extensions_config = extensions

    # 工具从配置里来（注册项写错、重名、name 不匹配、工厂参数写错都在这里炸）。必须在
    # smoke build 之前——smoke build 正是要拿这份列表去试装配。
    business_tools = build_tools(app_config)

    # 技能（T14）：把仓库里的源目录**镜像**到 `/skills` 的只读视图，再把清单交给建图工厂去拼进提示词。
    #
    # 放在**启动期**、`build_tools` 之后：技能是可选内容，坏了只记警告不拦启动（决议 #5），所以它
    # 与「上面的配置写错就拒绝启动」是两种性格；放在这里是因为它确定、快，且不需要事件循环。
    # 源在仓库里（进版本控制），视图在 `.evaai/` 下（运行期数据）——两层分工见 ADR-0018。
    # 沙箱布局（T17）：宿主根是**部署策略**，从这里构造一次、挂到 state 再往下发。
    # 工具侧不再有任何模块全局——它们从运行上下文拿到的就是这个对象（见 ADR-0020）。
    sandbox_layout = build_layout()
    app.state.sandbox_layout = sandbox_layout

    skills = project_skills(_resolve_path("skills"), sandbox_layout.skills_view_root())
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

        # docker 沙箱（T15）：只有 `tools:` 里真的装了 bash 才做这件事。启动期把「本部署建的、
        # 但会话已经不在 journal 里」的容器清掉——与 checkpoint 清理同属「外部资源的收尾」，
        # 所以失败也只警告、不拦启动。挂到 state 是为了让 DELETE 会话那条路由知道要不要收尾。
        app.state.bash_enabled = any(tool.name == BASH_TOOL_NAME for tool in business_tools)
        if app.state.bash_enabled:
            # 哨兵文件在这里建，不在建工具那一刻：启动期校验（`check_docker_ready`）跑在注册表
            # 里，那时还没有布局。见 `evaharness.sandbox.docker` 的模块说明。
            ensure_mount_check(sandbox_layout)
            await _reconcile_sandboxes(app.state.journal_store, sandbox_layout)

        # 建图的唯一入口。它只持有参数、不持有图：每个请求调 lead() 现建。
        # MCP：**连一次、把工具列表留下来**（之后就再不连了——运行期零 I/O，连接推迟到模型真的调
        # 那个工具时，见 `app.gateway.mcp`）。它与技能投影同属「外部内容」，所以失败也只警告不拦
        # 启动；与 smoke build 的分工是「我们自己的装配错→拒绝启动，外部依赖不可用→只警告」。
        #
        # 放在 AgentFactory 之前：工具表是构造参数，MCP 工具与业务工具在建图眼里没有区别。
        mcp_tools = await load_mcp_tools(extensions.mcp_servers, business_tools=business_tools)
        app.state.mcp_tools = mcp_tools

        # 知识库（T18）：把仓库 knowledge/ 投影到 /knowledge，建向量库与检索工具，并把
        # 「比对指纹 → 按文件增量重建」丢进**后台任务**——服务不等它（ADR-0022）。所以它与 MCP
        # 同属「外部内容」，失败只警告/在检索那一步可见地报错，不拦启动。工具是启动期常量，
        # 与业务工具、MCP 工具在建图眼里没有区别。
        knowledge = await start_knowledge(
            app_config.knowledge,
            source_root=_resolve_path(app_config.knowledge.source),
            view_root=sandbox_layout.knowledge_view_root(),
            persist_dir=_resolve_path(app_config.knowledge.persist_dir),
            fingerprint_db_path=_resolve_path(app_config.knowledge.fingerprint_db_path),
        )
        app.state.knowledge = knowledge
        knowledge_tools = [knowledge.tool] if knowledge is not None else []
        if knowledge is not None:
            stack.push_async_callback(knowledge.aclose)

        # 长期记忆（T21）：开三张空表，建**只含 memory_search** 的工具表、封注入渲染器与采集中间件，
        # 并起一个去抖抽取队列（每轮结束后后台调 LLM 整理画像与事实）。抽取器只在启用时构造：
        # 它要解析模型 profile，没开记忆的部署不该因为一个写错的 profile 名而起不来。
        memory_config = app_config.memory
        memory_extractor = make_extractor(app_config, memory_config) if memory_config.enabled else None
        memory = await start_memory(
            memory_config,
            db_path=_resolve_path(memory_config.db_path),
            extractor=memory_extractor,
        )
        app.state.memory = memory
        memory_tools = list(memory.tools) if memory is not None else []
        if memory is not None:
            stack.push_async_callback(memory.aclose)

        all_tools = [*business_tools, *mcp_tools, *knowledge_tools, *memory_tools]
        app.state.agent_factory = AgentFactory(
            app_config,
            all_tools,
            checkpointer=saver,
            skills=skills,
            memory=memory.render if memory is not None else None,
            memory_capture=memory.capture if memory is not None else None,
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
        smoke = AgentFactory(
            app_config,
            all_tools,
            checkpointer=None,
            skills=skills,
            memory=memory.render if memory is not None else None,
            memory_capture=memory.capture if memory is not None else None,
        )
        smoke.lead()

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


async def _reconcile_sandboxes(journal_store, layout) -> None:
    """启动时清理孤儿沙箱容器（本部署建的、会话已不在 journal 里的那些）。

    「会话在不在」以 journal 为准，不看 ``.evaai/user/{会话}/`` 目录——ADR-0012 记着
    「删会话不删文件」这条债，那个判据几乎永不命中。

    ``journal_store.threads()`` 失败就整个跳过，**不退化成按时间删**：删掉一个还在用的容器
    比留几个孤儿糟得多。失败只记日志——清理不该拦着服务起来。
    """
    try:
        live = {thread.thread_id for thread in await journal_store.threads()}
        removed = await asyncio.to_thread(docker.reconcile_orphans, layout, live)
    except Exception:
        logger.warning("沙箱容器清理失败，跳过", exc_info=True)
        return
    if removed:
        logger.info("已清理 %d 个孤儿沙箱容器", removed)


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
    # 记忆管理页（T20）走它：看 / 增 / 改 / 删事实与画像。
    app.include_router(memory.router)
    return app


app = create_app()
