"""知识库的**策略**侧：源目录从哪来、视图建在哪、用哪个嵌入模型、启动期怎么组装。

机制全在 :mod:`evaharness.knowledge`（切分 / 向量库 / 索引器 / 检索工具）——这一层只做三件
业务决定：

1. **投影**：把仓库根的 ``knowledge/`` 镜像到 ``/knowledge`` 的宿主视图（照 ``/skills`` 的做法，
   见 :func:`project_knowledge`）。
2. **嵌入**：按配置建一个 DashScope（OpenAI 兼容）嵌入客户端。
3. **组装 + 后台重建**：建库、建检索器与工具，把「比对指纹 → 按文件增量重建」丢进**后台任务**
   ——服务不等它，重启后第一条检索会经检索器的「就绪闸门」等构建完成（见 ADR-0022）。

与 MCP 一样，知识库工具是**启动期常量**：它与业务工具、MCP 工具并列进 ``AgentFactory.tools``，
所以主图与子图自动都有它，运行期零 I/O（连接推迟到工具真的被调时）。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import BaseTool
from langchain_openai import OpenAIEmbeddings

from evaharness.config.app_config import EmbeddingConfig, KnowledgeConfig
from evaharness.fingerprint import ChangeDetector, ChangeSet, SqliteFingerprintStore, fingerprint
from evaharness.knowledge import (
    SPLITTER_RECIPE,
    KnowledgeIndexer,
    KnowledgeRetriever,
    KnowledgeStore,
    make_knowledge_search,
)

logger = logging.getLogger(__name__)

_FINGERPRINT_NAME = "knowledge"
"""这份指纹在存储里的名字——索引「变了没」比的就是它。"""

VIEW_DIR_NAME = "knowledge-view"
"""``/knowledge`` 视图目录名，必须与挂载表里 ``Mount("/knowledge", parts=("knowledge-view",))`` 一致。"""


@dataclass
class KnowledgeRuntime:
    """启动期建好的知识库长命物，由 app 的 ``AsyncExitStack`` 持有到关机。"""

    tool: BaseTool
    retriever: KnowledgeRetriever
    store: KnowledgeStore
    fingerprints: SqliteFingerprintStore
    build_task: asyncio.Task[None]

    async def aclose(self) -> None:
        """收尾：停掉后台重建（若还在跑），关掉两个数据库连接。"""
        if not self.build_task.done():
            self.build_task.cancel()
            try:
                await self.build_task
            except asyncio.CancelledError:
                pass
        await self.store.close()
        self.fingerprints.close()


def project_knowledge(src_root: Path, view_root: Path) -> None:
    """把仓库的 ``knowledge/`` 镜像到 ``/knowledge`` 的只读视图。

    **镜像是为了「源是唯一真相」**：源里删掉的文档必须从视图里一并消失，否则视图第一次投影
    之后就开始自己长东西。与 :func:`app.gateway.skills.project_skills` 同一条取向——区别只在
    技能是「一级子目录各是一个包」，知识库是「整棵目录原样镜像」，不需要逐个校验名字。

    源目录不存在时**不创建视图**（如实报「不存在」比造个空目录好）；失败只记警告、不抛——它是
    可选内容，不该拦住服务起来。
    """
    if view_root.name != VIEW_DIR_NAME:
        # 这是**不可逆**的删除，一个算错的路径就会毁掉别处。守卫代价极低、后果极重。
        raise ValueError(f"拒绝投影到意外的目录名：{view_root}（应为 {VIEW_DIR_NAME}）")

    try:
        if view_root.exists():
            shutil.rmtree(view_root)
    except OSError as exc:
        logger.warning("知识库视图清不掉，本次不投影：%s（%s）", view_root, exc)
        return

    if not src_root.is_dir():
        logger.info("知识库源目录不存在，不创建 /knowledge 视图：%s", src_root)
        return

    try:
        shutil.copytree(src_root, view_root, symlinks=True, ignore_dangling_symlinks=True)
    except OSError as exc:
        logger.warning("知识库投影失败，本次没有 /knowledge 视图：%s（%s）", src_root, exc)


def build_embedder(config: EmbeddingConfig) -> OpenAIEmbeddings:
    """按配置建嵌入客户端（OpenAI 兼容协议，DashScope 直接可用）。

    ``check_embedding_ctx_length=False``：默认那条路会拿 tiktoken 估算长度并截断，而
    ``text-embedding-v4`` 不是 tiktoken 认识的模型；长度我们自己按字符控制在切分层已经管住了。
    ``chunk_size`` 是**每次请求的条数**（DashScope v4 实测上限 10），不是文本长度。
    """
    return OpenAIEmbeddings(
        model=config.model,
        base_url=config.base_url,
        api_key=config.api_key,
        dimensions=config.dimensions,
        chunk_size=config.batch_size,
        check_embedding_ctx_length=False,
    )


def index_recipe(config: KnowledgeConfig) -> dict[str, object]:
    """这份索引是「怎么算出来的」——嵌入模型、维度、切分参数。

    它比源目录指纹更根本：**源一个字没改，配方一变，已入库的向量也全部作废**（语义空间变了，
    或者维度对不上）。所以配方进向量库、启动时比对，不一致就整库重建。切分逻辑的版本号由
    :data:`~evaharness.knowledge.SPLITTER_RECIPE` 带着走——改了切分行为，版本号加一即可。
    """
    return {
        "embedding_model": config.embedding.model,
        "embedding_base_url": config.embedding.base_url,
        "dimensions": config.embedding.dimensions,
        "chunk_size": config.chunk_size,
        "chunk_overlap": config.chunk_overlap,
        "splitter": SPLITTER_RECIPE,
    }


async def start_knowledge(
    config: KnowledgeConfig,
    *,
    source_root: Path,
    view_root: Path,
    persist_dir: Path,
    fingerprint_db_path: Path,
) -> KnowledgeRuntime | None:
    """按配置把知识库建起来；``enabled`` 为假则返回 ``None``（不加任何工具）。

    ``api_key`` 缺失时**拒绝启动**（``enabled`` 已经是显式打开了，这时缺 key 是配置错，不是
    「功能没开」）——与「配置写错就拒绝启动」同口径。
    """
    if not config.enabled:
        logger.info("知识库未启用（knowledge.enabled=false），不加载检索工具")
        return None
    if not config.embedding.api_key:
        raise ValueError("knowledge.enabled=true 但 knowledge.embedding.api_key 为空（检查 .env 里的 key）")

    embedder = build_embedder(config.embedding)
    store = KnowledgeStore(persist_dir)
    await store.open()

    retriever = KnowledgeRetriever(store, embedder, top_k=config.top_k)
    tool = make_knowledge_search(retriever)
    fingerprints = SqliteFingerprintStore(fingerprint_db_path)

    project_knowledge(source_root, view_root)

    task = asyncio.create_task(
        _build_index(config, source_root=source_root, store=store, embedder=embedder,
                     retriever=retriever, fingerprints=fingerprints),
        name="evaai-knowledge-index",
    )
    return KnowledgeRuntime(
        tool=tool, retriever=retriever, store=store, fingerprints=fingerprints, build_task=task
    )


async def _build_index(
    config: KnowledgeConfig,
    *,
    source_root: Path,
    store: KnowledgeStore,
    embedder,
    retriever: KnowledgeRetriever,
    fingerprints: SqliteFingerprintStore,
) -> None:
    """后台任务：比对指纹，按文件增量重建索引，然后放行就绪闸门。

    **失败不吞**：异常会被记日志并交给检索器标成 ``failed``——此后每一次检索都**可见地报错**，
    而不是静默返回空结果（空结果与「索引是空的」在模型眼里无法区分，那是本项目拒绝的失败形状）。
    """
    retriever.mark_building()
    try:
        # 先校配方（顺带把集合建出来），再看源目录——源缺失时集合也已经存在了，检索会得空结果
        # 而不是撞一个「集合不存在」的错。
        recipe_ok = await store.ensure_recipe(index_recipe(config))
        if not source_root.is_dir():
            logger.warning("知识库源目录不存在，索引为空：%s", source_root)
            retriever.mark_ready()
            return

        # 指纹要扫盘、算 mtime/内容，所以丢线程——别占着事件循环（「别影响主进程」）。
        snapshot = await asyncio.to_thread(
            fingerprint, [source_root], ignore=tuple(config.ignore), mode=config.fingerprint_mode
        )
        if recipe_ok:
            changes = ChangeDetector(fingerprints).diff(_FINGERPRINT_NAME, snapshot)
        else:
            # 配方变了：旧向量全部作废，忽略指纹差异、全量重建。
            logger.info("索引配方变了（嵌入模型 / 维度 / 切分参数），丢弃旧向量、全量重建")
            changes = ChangeSet(added=snapshot.files)

        if changes.is_empty:
            logger.info(
                "知识库无变化（%d 个文件、库内 %d 个片段），跳过重建",
                len(snapshot.entries), await store.count(),
            )
        else:
            logger.info(
                "知识库有变动，增量重建：新增 %d / 修改 %d / 删除 %d",
                len(changes.added), len(changes.modified), len(changes.removed),
            )
            indexer = KnowledgeIndexer(
                store,
                embedder,
                source_root=source_root,
                chunk_size=config.chunk_size,
                chunk_overlap=config.chunk_overlap,
            )
            written = await indexer.apply(changes)
            logger.info("知识库索引重建完成：写入 %d 个片段，库内共 %d 个片段", written, await store.count())

        # 直接写、不丢线程：这个 sqlite 连接是在**事件循环线程**上建的（start_knowledge 里），
        # 跨线程用同一个连接会撞 sqlite 的线程亲和性检查。指纹的读写是毫秒级，不值得为它换线程；
        # 真正花时间的扫盘（fingerprint）与嵌入（网络）才是 to_thread / await 的对象。
        fingerprints.save(_FINGERPRINT_NAME, snapshot)
        retriever.mark_ready()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 兜住全部，转成「检索可见地失败」
        logger.exception("知识库索引构建失败，检索将报错")
        retriever.mark_failed(exc)


__all__ = ["KnowledgeRuntime", "VIEW_DIR_NAME", "build_embedder", "project_knowledge", "start_knowledge"]
