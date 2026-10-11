# 三期「Rag 知识库」切片：选型研究

> 研究时间：**2026-10-11**。选型对版本敏感，本文所有版本号与价格均为该时点。
>
> 约定说明：`docs/research/` 是本次**新建**的目录——此前仓库只在 `docs/` 下放 `adr/`（决策记录）
> 与 `agents/`（流程说明），没有「调研笔记」这一体裁。本文件是第一篇，规则是：**每条结论追到一手来源**
> （官方文档 / 源码 / 规范 / first-party API 文档），正文里给 Markdown 链接；只有二手来源的一律标注
> 「仅二手来源，未验证」。

---

## 0. 本项目的既有约束（决定选型的边界）

以下是调研前从仓库读到的事实，直接约束后面的比较，不重复论证：

| 约束 | 出处 | 对 RAG 的含义 |
|---|---|---|
| 主路径是 LangChain v1 `create_agent`，编译成 LangGraph 图 | `docs/adr/0003-reuse-langchain-create-agent.md` | RAG 只能长在「工具」或「中间件」两条缝上，没有第三条 |
| **每请求建图**，图/模型寿命 = 一次响应流 | `docs/adr/0011-per-request-graph-construction.md` | retriever/向量库**不能**建在请求路径上（会每请求开连接）；必须建在构造期、跨请求共享 |
| 业务工具**跨请求共享、必须无状态** | ADR-0011 §7 | 检索工具得像 MCP 工具那样是启动期常量，不许有每请求可变状态 |
| 持久化是 SQLite（checkpointer） | `docs/adr/0007-sqlite-checkpoint-persistence.md` | 技术栈已经有一个进程内、单机的数据库 |
| 沙箱是「每会话一个长驻 docker 容器 + bash + `/workspace`/`/output`/`/input`/`/skills` 挂载」 | `docs/adr/0019-docker-sandbox.md`、`docs/adr/0020-sandbox-in-harness.md` | 检索结果可以落 `/workspace`，把「正文」从模型上下文里挪走 |
| 有 MCP（`docs/adr/0017-mcp-connections.md`）与 skill（`docs/adr/0018-skill-loading.md`）两条「外部能力怎么接进来」的既有形状 | — | RAG 的接法应当**仿照**这两者，而不是新造一套生命周期 |
| 配置分三层：`config.yaml`（我们的工具）/ `extensions_config.json`（外部扩展）/ `.env` | ADR-0012 / 0017 | 知识库的旋钮（库路径、embedding profile、切分参数）该进哪一层要一起定 |
| 参照项目 DeerFlow 在 RAG 上**零实现** | 用户给定 | 没有可对照的先例，只能靠外部调研 |

**依赖基线**（本机 `backend/.venv` 实读，`uv pip list`，2026-10-11）：

| 包 | 版本 | 备注 |
|---|---|---|
| `langchain` | 1.4.2 | 顶层只导出 `create_agent` / `AgentState`（`langchain/agents/__init__.py`） |
| `langchain-core` | 1.6.5 | `Document` / `BaseRetriever` / `VectorStore` / `InMemoryVectorStore` 在此 |
| `langgraph` | 1.2.12 | |
| `langchain-openai` | 1.6.6 | `OpenAIEmbeddings` / `ChatOpenAI` 可用 |
| `langgraph-checkpoint-sqlite` | 3.1.1 | **它的依赖里已经带着 `sqlite-vec`** |
| `sqlite-vec` | 0.1.9 | 作为上一条的传递依赖**已装在 venv 里** |
| `langchain-text-splitters` | **未安装** | 要用得显式 `uv add` |
| `langchain-classic` | 未安装 | v1 把 legacy retrievers 搬到了这里 |
| `langchain-community` | 未安装 | 官方已标「不再维护」 |

> 本机实测（下文「本机实测」均指此环境）：
> - `from langchain_core.tools.retriever import create_retriever_tool` 可用；
>   `from langchain import create_retriever_tool` **失败**（v1 顶层不再 re-export）。
> - `sqlite-vec` 加载成功：`vec_version = v0.1.9`，本机 SQLite `3.42.0`。
> - `sqlite-vec` 在 **aiosqlite** 下可用：`await db.load_extension(sqlite_vec.loadable_path())` 之后
>   `vec0` 虚拟表建表、插入、KNN 查询全部跑通（直接传 `db._conn` 给 `sqlite_vec.load()` 会撞
>   SQLite 的线程亲和性错误，必须走 `load_extension` 这条路）。

---

## 1. 现成形状：LangChain v1 生态的 RAG 组件（问题 1）

### 1.1 v1 的包边界被**重划**了，这是本项目最需要先知道的一件事

LangChain v1 官方迁移指南白纸黑字：**`langchain` 包收窄到 agents / messages / tools / chat models /
embeddings**；「legacy chains、**retrievers**、indexes、hub、`CacheBackedEmbeddings`、community re-export
**全部搬到 `langchain-classic`**」。[LangChain v1 迁移指南](https://docs.langchain.com/oss/python/migrate/langchain-v1)（一手）

这意味着**记忆里那些 2023–2024 的 import 路径基本都失效了**。实测/查证到当前（2026-10）的真实分布：

| 组件 | 现在住哪 | 依据 |
|---|---|---|
| `Document`、`Embeddings`、`VectorStore`、`BaseRetriever`、`InMemoryVectorStore` | `langchain_core.*` | 本机 import 实测；[knowledge-base 教程](https://docs.langchain.com/oss/langchain/knowledge-base) |
| `create_retriever_tool` | `langchain_core.tools.retriever`（签名见下） | 本机源码 `langchain_core/tools/retriever.py` |
| `create_agent`、`@tool`、中间件 | `langchain.agents` / `langchain.agents.middleware` / `langchain.tools` | 本机 `langchain/agents/__init__.py` |
| `EnsembleRetriever`、`ContextualCompressionRetriever`、`MultiQueryRetriever` | **`langchain-classic`**（新装一个包） | 迁移指南 §"langchain-classic"；维护者回复见 [langchain#33292](https://github.com/langchain-ai/langchain/issues/33292) |
| `BM25Retriever` | `langchain_community.retrievers`（**未维护**） | 本机拉取源码 `.../retrievers/bm25.py` 确认 |
| `Chroma` | `langchain-chroma`（1.1.0，活跃） | [Chroma 集成文档](https://docs.langchain.com/oss/python/integrations/vectorstores/chroma) |
| `PGVector` | `langchain-postgres`（0.0.19） | [PGVector 集成文档](https://docs.langchain.com/oss/python/integrations/vectorstores/pgvector) |
| `SQLiteVec` / `FAISS` / `LanceDB` | `langchain_community.vectorstores`（**未维护**） | 拉取 `.../vectorstores/{sqlitevec,faiss,lancedb}.py` 确认存在 |

`langchain-community` 的状态是官方的：集成索引页顶部直接挂警告
「**langchain-community 包不再维护；从它 import 的例子可能过时或已坏**」
（[向量库索引](https://docs.langchain.com/oss/integrations/vectorstores)）。
「用 sqlite-vec / LanceDB / FAISS 就得吃一个不维护的包」是本节最重要的一个代价。

### 1.2 官方给的 RAG 架构分类（这直接回答「主流形状有哪些」）

[Retrieval 总览](https://docs.langchain.com/oss/deepagents/retrieval)把 RAG 分成三类，并给了控制力/灵活性/延迟的对照：

| 架构 | 描述 | 控制力 | 灵活性 | 延迟 |
|---|---|---|---|---|
| **2-Step RAG** | 检索**永远**在生成之前，固定管线 | 高 | 低 | 快且**可预测**（最多几次 LLM 调用已知） |
| **Agentic RAG** | agent 在推理中自己决定**何时、如何**检索（= 把检索做成工具） | 低 | 高 | 可变 |
| **Hybrid** | 两者混合，加查询改写/检索校验/答案校验等中间步 | 中 | 中 | 可变 |

文档明确：「agent 要获得 RAG 行为，**唯一需要的东西**就是能取外部知识的一个或多个**工具**」。

### 1.3 能和 `create_agent` 直接整合的部分

- **检索工具（Agentic RAG）**：一个 `@tool`，内部 `retriever.invoke(query)`，把结果拼成字符串返回。
  `create_agent(tools=[...])` 直接吃。这是**零适配**的接法。
- **被动注入（中间件）**：`AgentMiddleware` 有 `before_agent` / `before_model` / `after_model` /
  `wrap_model_call` / `wrap_tool_call` / `after_agent` 六个钩子
  （本机源码 `langchain/agents/middleware/types.py`；[中间件总览](https://docs.langchain.com/oss/langchain/middleware/overview)）。
  `wrap_model_call` 里改 `request.messages` 就能在发模型前塞进检索到的上下文
  （官方在 [context-engineering](https://docs.langchain.com/oss/langchain/context-engineering) 里给了
  `inject_file_context` 的完整范式）。
- **没有现成的「向量 RAG 中间件」**：`langchain.agents.middleware` 里唯一沾「搜索」的是
  `FilesystemFileSearchMiddleware`，而它提供的是 **Glob/Grep 工具**（对文件做关键词/正则搜索），
  不是向量检索（本机源码 `middleware/file_search.py` 首行自述）。想要「被动向量注入」，**得自己写中间件**。

### 1.4 一个值得注意的转向：官方教程现在**不推荐** `create_retriever_tool`

`create_retriever_tool` 在核心里仍然存在，签名是
`create_retriever_tool(retriever, name, description, *, document_prompt=None, document_separator="\n\n", response_format="content") → StructuredTool`（本机源码）。
但**当前的 agentic-RAG 官方教程**里，那份被命名为 `agentic-rag-create-retriever-tool-py` 的代码片段
**根本没用它**，而是手写一个 `@tool`：

```python
@tool
def retrieve_blog_posts(query: str) -> str:
    """Search and return information about Lilian Weng blog posts."""
    retrieved_docs = _get_retriever().invoke(query)
    return "\n\n".join([doc.page_content for doc in retrieved_docs])
```

（[来源](https://github.com/langchain-ai/docs/blob/main/src/snippets/code-samples/agentic-rag-create-retriever-tool-py.mdx)）。
**结论**：检索工具的**可定制性**（自己决定返回什么、要不要写文件、要不要带元数据引用）比用工厂函数更重要——
这与我们「工具返回的是指针而不是正文」的思路一致（见 §6）。

---

## 2. 向量存储（问题 2）

维度：部署运维复杂度 / 并发与多进程 / 元数据过滤 / 与 LangChain retriever 接口的适配 / 能否塞进现有 SQLite 体系。

| | **sqlite-vec** | **LanceDB** | **Chroma** | **FAISS** | **pgvector** |
|---|---|---|---|---|---|
| 形态 | SQLite 扩展（纯 C，无依赖） | 嵌入式 Rust 库（Lance 列存） | 嵌入式库 / 单机 server | 算法库（非数据库） | Postgres 扩展 |
| 当前版本 | 0.1.10-alpha.4（**pre-v1**，README 明说「expect breaking changes」） | lancedb 0.40.0 | chromadb 1.5.9 | faiss-cpu 1.15.1 | v0.8.7 |
| 部署运维 | **零新增**——扩展已在 venv（`langgraph-checkpoint-sqlite` 带进来），本机已实测可加载 | 零服务，`pip install lancedb`，数据是一个目录 | 零服务（嵌入式），或 `chroma run` 起单机 server | 零服务，但**要自己管持久化与元数据** | **要一整个 Postgres 进程**（官方文档给 `docker run pgvector/pgvector:pg16`） |
| 并发/多进程 | 受 SQLite 并发模型约束（单写者）；扩展本身是纯函数 | 「多读者安全，多写者要外部协调（乐观并发，撞了抛 `CommitConflictError`）」 | HNSW 索引**必须常驻 RAM**；查询并行度 = vCPU 数；官方建议批量 50–250 | 进程内库，无并发语义（要自己加锁） | ACID，Postgres 全功能 |
| 元数据过滤 | **列式**：`metadata columns`（可进 KNN 的 `WHERE`）、`auxiliary columns`（不可进 WHERE）、`partition key`（分片） | SQL/Arrow filter，可 `where` | 声明式 `filter={"k": v}`（支持 `$in` 等），另有全文/正则 | **无**（只是向量，元数据得自己存另一处） | 声明式 `filter={"id": {"$in": [...]}}` |
| LangChain 适配 | `langchain_community.vectorstores.SQLiteVec`（**未维护**，同步 sqlite3） | `langchain_community.vectorstores.LanceDB`（**未维护**） | **`langchain-chroma`（一手、活跃）** | `langchain_community.vectorstores.FAISS`（**未维护**） | **`langchain-postgres`（一手）** |
| 塞进现有 SQLite 体系 | **能**——同一个 `.db` 文件、同一套连接 | 否（独立格式/目录） | 部分——Chroma 自己也用 SQLite 存文档与元数据 | 否 | 否 |

一手来源：
[sqlite-vec README](https://github.com/asg017/sqlite-vec)（pre-v1 与形态）、
[sqlite-vec vec0](https://alexgarcia.xyz/sqlite-vec/features/vec0.html)（三类非向量列）、
[sqlite-vec KNN](https://alexgarcia.xyz/sqlite-vec/features/knn.html)、
[LanceDB README](https://github.com/lancedb/lancedb)、
[LanceDB 增量重索引](https://docs.lancedb.com/indexing/reindexing)（多写者/并发说明见搜索到的 LanceDB 文档摘要——**该并发结论仅二手来源，未验证**）、
[Chroma 架构](https://docs.trychroma.com/reference/architecture/overview)、
[Chroma 单机性能](https://docs.trychroma.com/guides/performance/single-node)、
[FAISS README](https://github.com/facebookresearch/faiss)、
[pgvector README](https://github.com/pgvector/pgvector)、
[PGVector 集成文档](https://docs.langchain.com/oss/python/integrations/vectorstores/pgvector)。

### 2.1 几个关键事实

- **sqlite-vec 没有 ANN 索引，是暴力扫描**（README 自称 "fast enough"；文档只给 `vec0` 暴力 KNN，无 IVF/HNSW）。
  它另有 binary/scalar quantization、Matryoshka 降维等优化手段，但没有近似索引。中小规模（几千~几万 chunk）够用，
  再大就没有加速结构可调。
- **sqlite-vec 是唯一「零新增基础设施」的选项**：它已经因为 `langgraph-checkpoint-sqlite` 躺在 venv 里，
  **本机已验证能加载并在 aiosqlite 下跑通 KNN**。这与「单机 + SQLite + 尽量不加进程」的现状最贴。
  代价是**扩展是 pre-v1、且 LangChain 适配器在未维护的 community 包里**。
- **Chroma 与 pgvector 是「有一手 LangChain 适配器」的两个**（`langchain-chroma` / `langchain-postgres`）。
  但 Chroma 的向量索引**必须常驻内存**（官方性能页），几百 MB~几 GB 的 RAM 是硬开销；
  pgvector 要拉一个 Postgres 进程，与「单机个人助手」的重心相反（官方搭车文档就是 `docker run` 一个 pg）。
- **FAISS 是「不是数据库的向量库」**：没有元数据、没有持久化、没有删除/过滤语义，且适配器也在未维护的包里。
  拿来当库可以，当知识库底座不合适。
- **元数据过滤的形状差异很大**：Chroma/pgvector 是**声明式 dict**（`filter={...}`，能下推到存储层）；
  `langchain_core.vectorstores.InMemoryVectorStore` 的 `filter` 是**一个 Python 谓词**
  `Callable[[Document], bool]`（本机源码），是**客户端后过滤**；sqlite-vec 是**列谓词**（最接近我们的库内过滤）。
- 用 sqlite-vec/LanceDB/FAISS 就意味着 import 未维护的 `langchain-community`；
  用 Chroma/pgvector 则引一个新的**一手包**（`chromadb` / `psycopg` 一族）。

---

## 3. 嵌入（问题 3）

> 各厂商在 2026-10 已比「记忆里的版本」换代一到两代（Cohere 到 Embed 5、Jina 到 v5、Voyage 到 v4、
> DashScope 到 v4）。下表只列能追到**厂商 doc** 的数字；标注「(仅二手)」的一律未验证。**取 key 时以官方价目为准。**

### 3.1 在线 API

| 厂商 | 当前模型 | 维度 | 价格（每 1M input token） | 单条 max token / 每次 batch | OpenAI 兼容 |
|---|---|---|---|---|---|
| **OpenAI** | `text-embedding-3-small` / `-large` | 1536 / 3072，`dimensions` 可降（Matryoshka） | **$0.02 / $0.13** | 8,191 tok/条；≤2,048 条/请求（**仅二手**） | 原生 |
| **DashScope** | `text-embedding-v4`、`qwen3.7-text-embedding`（v3 为 legacy） | v4: 默认 1024，可选 2048…64；qwen3.7: 默认 1024，可选 2560…256 | **$0.07/1M**；免费额度 1M token（90 天） | v4: 8,192 tok/条、10 条/请求；qwen3.7: 128,000 tok/条、20 条/请求 | **是**（compatible-mode 端点） |
| **Jina** | `jina-embeddings-v5-text-small`(677M) / `-nano`(239M) | 1024 / 768，MRL，32k 序列 | ~$0.05 / $0.02（**仅二手**，定价页当时 404） | 32k tok/条 | 大体兼容（`/v1/embeddings`） |
| **Cohere** | `embed-v5.0-pro` / `embed-v5.0-fast`、`embed-v4.0` | 默认 2048（v4 默认 1536），128k 上下文 | **$0.12 / $0.08** | **≤96 条/请求** | **否**（自家 `/v2/embed`） |
| **Voyage**（MongoDB 旗下） | `voyage-4-large` / `voyage-4` / `voyage-4-lite` | 默认 1024，可选 256/512/2048 | **$0.12 / $0.06 / $0.02**；**每账号前 200M token 免费** | 32k tok/条；**≤1,000 条/请求** | **否**（自家 SDK） |
| **SiliconFlow**（硅基流动，中国聚合） | 托管 `BAAI/bge-m3`、`BAAI/bge-large-zh-v1.5`、`Qwen3-Embedding 0.6B/4B/8B` | 见 §3.2 | bge-m3 / bge-large-zh-v1.5 **标价 0（免费）**；Qwen3-Emb ¥0.07~0.28/1M | 页面未列硬上限 | **是**（`api.siliconflow.cn/v1`） |

来源：OpenAI [pricing](https://developers.openai.com/api/docs/pricing) / [guide](https://developers.openai.com/api/docs/guides/embeddings)；
DashScope [sync API](https://www.alibabacloud.com/help/en/model-studio/text-embedding-synchronous-api)；
Jina [v5 card](https://huggingface.co/jinaai/jina-embeddings-v5-text-small) / [embeddings](https://jina.ai/embeddings)；
Cohere [models](https://docs.cohere.com/docs/cohere-embed) / [pricing](https://cohere.com/pricing)；
Voyage [embeddings](https://docs.voyageai.com/docs/embeddings) / [pricing](https://docs.voyageai.com/docs/pricing)；
SiliconFlow [CN models](https://siliconflow.cn/models?types=embedding)。
**关键坑**：Jina v5-text 是 **CC BY-NC 4.0（非商用）**，商用要联系销售；Cohere/Voyage **不是** OpenAI 接口。

### 3.2 本地 / 自托管模型

| 模型 | 参数 | 维度 | 磁盘 | 中文指标（作者公布） | 许可 | 推理依赖 |
|---|---|---|---|---|---|---|
| `BAAI/bge-m3` | ~568M | 1024 | 2.27GB(fp32)/~1.1GB(fp16) | MIRACL/MKQA 多语领先；100+ 语言 | **MIT** | FlagEmbedding / sentence-transformers / transformers |
| `BAAI/bge-large-zh-v1.5` | ~326M | 1024 | 1.3GB | **C-MTEB avg 64.53**；中文专用 | **MIT** | 同上 |
| `BAAI/bge-small-zh-v1.5` | ~24M | 512 | **95.8MB** | C-MTEB avg 57.82 | **MIT** | 同上（CPU 极友好） |
| `Qwen/Qwen3-Embedding-0.6B` | 0.6B | 1024（MRL 32–1024） | 1.19GB | MTEB-multi 64.33 / **C-MTEB 66.33** | **Apache-2.0** | transformers / sentence-transformers |
| `Qwen/Qwen3-Embedding-4B` | 4B | 2560 | 8.04GB | MTEB-multi 69.45 / **C-MTEB 72.27** | Apache-2.0 | 同上 |
| `Qwen/Qwen3-Embedding-8B` | 8B | 4096 | 15.13GB | **MTEB-multi 70.58（2025-06 榜首）** / **C-MTEB 73.84** | Apache-2.0 | 同上 |
| `intfloat/multilingual-e5-large` | ~560M | 1024 | 2.24GB | 多语 70.5；C-MTEB 58.79（BGE 卡的对比值） | MIT | sentence-transformers |

来源：[bge-m3](https://huggingface.co/BAAI/bge-m3)、[bge-large-zh-v1.5](https://huggingface.co/BAAI/bge-large-zh-v1.5)、
[bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5)、[Qwen3-Embedding-8B](https://huggingface.co/Qwen/Qwen3-Embedding-8B)（C-MTEB 表在其卡内）、
[multilingual-e5-large](https://huggingface.co/intfloat/multilingual-e5-large)。
中文 prompt 前缀：BGE-zh 系列用「`为这个句子生成表示以用于检索相关文章：`」；Qwen3-Embedding 支持
instruct（作者称多数任务里加指令 +1%~5%，且**指令建议用英文**）。

### 3.3 LangChain 侧接法

- **本地**：`from langchain_huggingface import HuggingFaceEmbeddings`（一手包 **`langchain-huggingface` 1.2.3**，
  依赖 `sentence-transformers>=5.2,<6`）；远程 TEI 用同包的 `HuggingFaceEndpointEmbeddings`
  （[源码](https://github.com/langchain-ai/langchain/blob/master/libs/partners/huggingface/langchain_huggingface/embeddings/huggingface.py)）。
- **在线**：OpenAI / DashScope / SiliconFlow 走 `langchain_openai.OpenAIEmbeddings(base_url=...)`；
  Cohere / Voyage 要各自的 partner 包。
- 官方推荐从 [MTEB leaderboard](https://huggingface.co/spaces/mteb/leaderboard) 起步、再用自己的数据小样本评估
  （[embeddings 索引](https://docs.langchain.com/oss/integrations/embeddings)）。

### 3.4 选型要点（事实层面）

- **中文**：`bge-m3`、`bge-*-zh`、`Qwen3-Embedding`、DashScope `text-embedding-v4`、SiliconFlow（免费托管）都有明确中文支持。
- **OpenAI 兼容**：OpenAI / DashScope / SiliconFlow 可被 `OpenAIEmbeddings` 直调；Cohere / Voyage 不行。
- **本地模型的推理依赖**：`sentence-transformers` 会拉 `torch`（CPU 版体积大）；0.6B 级 CPU 可跑，8B 级不现实；
  中文小到 `bge-small-zh-v1.5`（95.8MB）也能用。`model2vec`（0.10.0）是「无 torch」路线，但模型面窄。

---

## 4. 切分与解析（问题 4）

### 4.1 Splitter：LangChain 的现状

`langchain-text-splitters` 是**独立包**（当前 1.1.3，[PyPI](https://pypi.org/project/langchain-text-splitters/)），
本项目**尚未安装**，要用得显式 `uv add`。官方推荐的默认是
`RecursiveCharacterTextSplitter`（[splitters 索引](https://docs.langchain.com/oss/integrations/splitters)）：
按 `\n\n → \n → 空格 → 字符` 递归，尽量保住大块。

结构感知（markdown）用 **`MarkdownHeaderTextSplitter`**：按标题层级切，把标题路径写进每个 chunk 的
`metadata`（如 `{'Header 1': 'Foo', 'Header 2': 'Bar'}`）；默认 `strip_headers=True`（标题行从正文剥离），
且**默认会吃掉空白与换行**——要保原样得用 `ExperimentalMarkdownSyntaxTextSplitter`
（[Markdown 切分文档](https://docs.langchain.com/oss/python/integrations/splitters/markdown_header_metadata_splitter)）。
两份 splitter 可**串联**：先按标题切成 section、再对每组跑 `RecursiveCharacterTextSplitter` 控长。

同页给的**故障排查**很关键：header 切分后 `chunk_overlap` 只在**单个 section 内**生效，
**不跨**标题边界；且要 `split_documents(docs)`（不是 `split_text`）才会在每组内应用 overlap。

### 4.2 中文感知的切分

源码级确认（`langchain_text_splitters/character.py`）：`RecursiveCharacterTextSplitter` 的**默认分隔符
是 `["\n\n", "\n", " ", ""]`**——**纯英文/空白导向，没有任何中文标点**。而那个 `from_language(...)`
classmethod 里的 `Language` 枚举是**编程语言**（给代码切分用），**不是自然语言**——所以
**LangChain 不提供中文自然语言分隔符**，必须自己传 `separators`
（[源码 character.py](https://github.com/langchain-ai/langchain/blob/master/libs/text-splitters/langchain_text_splitters/character.py)、
[base.py](https://github.com/langchain-ai/langchain/blob/master/libs/text-splitters/langchain_text_splitters/base.py)）。

中文的通行做法（**属工程惯例，LangChain 文档未收录，是实践建议而非文档结论**）：
`separators=["\n\n", "\n", "。", "！", "？", "；", "，", ""]`，让递归优先在中文句末断开，末尾留 `""` 兜底。
中文无空格分词，**按字符 / 按 token 的长度切分比按「词」可靠**；`TokenTextSplitter`（tiktoken 编码，
对中文是逐字/BPE）比 `CharacterTextSplitter` 更贴近模型实际 token 数。

语义切分：LangChain 有 `SemanticChunker`（实验性，community/experimental 一带），
但**不在 v1 核心的一手文档里**；对中小库的收益存疑，见 §5。

### 4.3 解析（PDF / DOCX）

- **PDF**：`pypdf`（官方 knowledge-base 教程用的就是它，纯 Python，快，但对复杂版式/表格/双栏弱）；
  `PyMuPDF/fitz`（快、版式好，**许可为 AGPL-3.0 或商业双授权**，本机 PyPI 实读 v1.28.2，注意传染性）；
  `pdfplumber`（表格强、慢，**MIT**）；`unstructured`（**Apache-2.0**，重、需模型）；`docling`（版式最强，重）。
  许可在本机 PyPI 实读确认：`PyMuPDF 1.28.2 = AGPL-3.0/商业`、`pdfplumber 0.11.10 = MIT`、
  `unstructured 0.27.25 = Apache`。CJK 支持取决于是否内嵌字体与坐标算法，`pypdf.extract_text()` 对 CJK 常见乱序。
  官方教程明确演示用 `pypdf`（[knowledge-base](https://docs.langchain.com/oss/langchain/knowledge-base)）。
- **DOCX**：`python-docx`（**MIT**，本机 v1.2.0）/ `docx2txt` / `unstructured`。LangChain 侧对应加载器在
  `langchain_community.document_loaders`（`Docx2txtLoader` / `UnstructuredWordDocumentLoader`），
  **同样是未维护包**——解析器本身可以直用，只是别依赖 community 包当门面。
- **现状判断**：项目已有 `markdownify` + `readabilipy`（HTML→Markdown / 正文抽取）在 app 层。
  markdown 是**最容易做对**的格式（标题结构现成、切分器一手支持）；PDF/DOCX 才是难点，且没有「零依赖且质量好」
  的选项——`pypdf` 够用但有损，重型方案要拖 `unstructured`/`docling` 与它们的模型依赖。

---

## 5. 检索：纯向量 vs 混合 vs 重排（问题 5）

### 5.1 有实测数据的一手来源

**Anthropic「Contextual Retrieval」**（一手，2024-09 发布，[原文](https://www.anthropic.com/news/contextual-retrieval)）
给的 top-20 检索失败率降幅（在「先给每个 chunk 补一句 LLM 生成的上下文再接索引」的前提下）：

| 配置 | 失败率降幅 |
|---|---|
| Contextual Embeddings 单独 | **35%**（5.7% → 3.7%） |
| Contextual Embeddings + Contextual **BM25**（混合） | **49%**（5.7% → 2.9%） |
| 上面两者 + **Reranking** | **67%** |

**读法**：在「已经做了 contextual 化」的强前提下，**混合检索**才把失败率从 35% 拉到 49%，**重排**再拉到 67%。
三个改动是**叠加**的，且重排是最后一步、收益也最大的一档。注意这是 Anthropic 自己的语料/口径，**不是在我们库上**。

**BGE 作者**（一手，[BGE-M3 模型卡](https://huggingface.co/BAAI/bge-m3)）的建议管线是
**「hybrid retrieval + re-ranking」**，并解释 cross-encoder reranker 精度高于 bi-encoder，
检索后加 reranker 能进一步筛选。

### 5.2 在「中小规模（几千~几万 chunk）」上的实际判断

- **纯向量**在语义匹配上够用，但**专名/编号/代码/罕见词**这类「字面命中」场景会漏；这是混合检索的动机。
- **混合（向量 + BM25/全文）**：收益最确定的场景是「查询里有精确 token」。
  **但 BM25 需要一份独立的全文索引**——这是我们技术栈里要多出来的东西（sqlite 可以用 FTS5 补，
  但要自己接融合；Chroma/LanceDB 自带 full-text；`BM25Retriever` 在未维护包里）。
  **一个省事的旁路**：`bge-m3` 在一次前向里**同时输出 dense + sparse（类 BM25 的 token 权重）+ multi-vector**，
  「不用额外代价就拿到稀疏权重」是作者的原话（[BGE-M3 卡](https://huggingface.co/BAAI/bge-m3)、
  [论文 2402.03216](https://arxiv.org/abs/2402.03216) 的 Multi-Functionality/self-knowledge-distillation）——
  若选它当 embedding，混合检索的「词法侧」可以复用同一模型，不必另建 BM25 索引。
- **重排**：把 top-N（如 20~50）用 cross-encoder 重排到 top-k，**延迟换精度**。
  本地 reranker（`bge-reranker-v2-m3`，Apache-2.0，~568M 参数、`use_fp16=True` 可加速；模型卡里
  **多语、中英、以及「要效率」三种场景都推荐它**，
  [模型卡](https://huggingface.co/BAAI/bge-reranker-v2-m3)）要**额外一次模型前向**；
  在线 reranker 是一次网络往返 + 计价——Cohere Rerank 当前模型是
  **`rerank-v4.0-pro` / `rerank-v4.0-fast`、`rerank-v3.5`（多语）、`rerank-english-v3.0` / `rerank-multilingual-v3.0`**，
  官方称训练覆盖 100+ 语言（[Cohere Rerank 文档](https://docs.cohere.com/docs/rerank-overview)）。
- **规模不敏感，但延迟敏感**：几千~几万 chunk 时，向量检索本身是毫秒级；
  重排的**固定开销**（一次 cross-encoder 推理 / 一次 API 调用）是主要代价。是否值得要看对「答案必须踩在原文上」
  的要求有多硬。
- 一手**数字对照**目前只有 Anthropic 那份能直接引用；其余多为二手博客，**不引用**。结论方向一致：
  **重排 > 混合 > 纯向量，单个改动的量级是「个百分点级」，且强依赖语料**。

---

## 6. 集成形态：工具 vs 中间件 vs 混合（问题 6）

### 6.1 三条路的代价（对照 §1.2 官方分类）

| 形态 | 谁决定检索 | 上下文预算 | 延迟 | 可控性 | 在本项目 |
|---|---|---|---|---|---|
| **2-Step（中间件被动注入）** | 每次运行都注入 | **每次请求都吃掉一段**（无法按需） | **固定 +1 检索**，可预测 | 高（我们能强制） | 需自写中间件；会把检索延迟加到**每一轮** |
| **Agentic（工具）** | 模型自己决定调不调、调几次 | **按需**（不调=零成本） | 可变（多轮工具调用） | 低（模型可能不调/乱调） | 最省适配，`@tool` 即插 |
| **Hybrid** | 先规则/分类，再按需 | 中 | 可变 | 中 | 需要额外的判定节点 |

官方对此的定性（[Retrieval 总览](https://docs.langchain.com/oss/deepagents/retrieval)）：
2-Step「延迟更可预测」，Agentic「灵活性高、延迟可变」。

### 6.2 官方给 agent 的推荐形态：**工具 + 把 chunk 落到文件系统 + 子 agent 分析**

[Deep Agents 的 RAG 教程](https://docs.langchain.com/oss/deepagents/rag) 里有一个叫
**「retrieve, offload, and delegate」**的模式，摘要原文：agent 检索匹配的 chunk 后**把它们写进文件系统，
而不是把全文留在编排者的上下文里**；子 agent 并行读取、搜索、总结各个文件。
其代码片段正是：检索工具跑 `vector_store.similarity_search(query, k=4)`，把每个 chunk 写成
`/retrieved/{batch_id}/chunk_{i}.md`，**只返回文件路径**（[snippet](https://github.com/langchain-ai/docs/blob/main/src/snippets/code-samples/rag-deep-search-tool-py.mdx)）。

> **这条与 EvaAI 现有形状高度吻合**：我们本来就有「每会话长驻 docker 沙箱 + `/workspace` 挂载」与
> 「子 agent 上下文隔离」两样东西。把检索结果写 `/workspace`、把「读全文/精读」派给子 agent，
> **不发明任何新机制**，直接用既有沙箱与 `task` 工具。

### 6.3 在「每请求建图」下，retriever 该建在哪一层

- **不能建在请求路径上**：每请求建图（ADR-0011）会把「每请求 new 一个向量库客户端 / 打开一个连接」
  变成常态。ADR-0011 已经把这个教训写死过一次（模型/图每请求建、但连接池不能每请求建）。
- **应当像 MCP 工具一样，是启动期常量**：ADR-0017「补记二」把 MCP 从「每运行建连」改成
  「启动期连一次、工具列表留下」——理由（建连贵、工具是数据、执行时才需要连接）**逐条适用于 retriever**。
  所以：**embedding 客户端 + 向量库句柄 + retriever + 检索工具**全部在 lifespan 构造、
  经 `AgentFactory` 持有、跨请求共享（与 `BUSINESS_TOOLS` 同一条路）。
- **两条缝的落点**：
  - 工具路：在 `config.yaml` 的 `tools:` 里加一项，`use:` 指向一个 `make_kb_search(...)` **工厂**
    （工厂的额外键 = 构造参数，ADR-0012 的既有约定），由注册表在启动期建出 `BaseTool`。
    **现成先例是 `web_search`**：`use: app.tools.web:make_web_search` + `max_results`/`timeout` 两个额外键，
    在启动期绑进闭包（本机 `config.yaml` 实读）。知识库检索工具照抄这个形状即可，不用新造接缝。
  - 中间件路：若要被动注入，得在 `AgentFactory._lead_middlewares` 的链里加——**但这条会顶破**
    `tests/test_react.py` 里「默认中间件链六个类名」的硬断言与
    `tests/test_lead_agent_memory.py` 的「模型收到 10 条消息」断言（ADR-0018 §5 记过同类冲突）。
    也就是说：**往中间件链里加东西有既有测试成本**，往 `tools:` 里加没有。

---

## 7. 知识库的接入与管理（问题 7）

### 7.1 文档从哪来

两条现成的口子：
- **用户上传**：`/input` 挂载（只读、会话相关）已经在挂载表里（ADR-0019/0020）。会话级、临时性。
- **目录挂载**：启动期从宿主目录投影到某处——形状**照抄 skill 的做法**（ADR-0018：
  仓库 `skills/<名>/SKILL.md` → 启动期镜像到 `/skills` 只读视图；源是唯一真相，删除即失效）。
  知识库源目录可以走同一条「源目录 + 启动期投影」，把「知识从哪来」与「索引怎么建」解耦。

### 7.2 索引启动期建 vs 按需建

- **启动期建**：与技能/配置「运行期不扫、改了要重启」的既有立场一致（ADR-0018 §7）。
  好处是「这次运行能看到哪些知识」是确定的；坏处是**大库会把启动变慢**。
- **按需建/增量**：LangChain 的 `VectorStore.add_documents(documents, ids=...)` 与 `delete(ids)` 是统一接口
  （[向量库索引](https://docs.langchain.com/oss/integrations/vectorstores)），
  Chroma 支持 `add_documents(..., ids=[uuid...])` 做**显式 id**（[Chroma 集成](https://docs.langchain.com/oss/python/integrations/vectorstores/chroma)），
  LanceDB 支持 `table.optimize()` 做**增量重索引**、重索引期间「旧索引 + 新数据的暴力扫描」合并结果、
  默认保留 7 天版本（[LanceDB 重索引](https://docs.lancedb.com/indexing/reindexing)）。
- **失效（invalidation）**：统一做法是**用内容指纹当 id**——文档变了 id 就变，旧 chunk 按 id 删、新 chunk 按 id 增。
  这条要靠我们自己实现，LangChain 不给「源目录 → 索引」的同步器（v0 时代的 `indexes` 模块已搬到
  `langchain-classic`，且是 index 工具而非知识库同步器）。

### 7.3 与配置分层的关系（需要一起定，见 §8）

新增的知识库旋钮（库路径、embedding profile、切分参数、源目录、是否启动期建）该放哪层，三种口径都有先例：
`config.yaml`（我们的工具）、`extensions_config.json`（外部扩展）、或「源目录即真相」（照 skill）。

---

## 8. 需要用户决策的真实分叉

> 每点给选项与代价，**不替用户拍板**。

**决策 1：向量库底座——「零新增设施」还是「一手适配器」？**
- A. **sqlite-vec**：唯一能塞进现有 SQLite 体系、且**已经在 venv 里、本机已实测可跑**；代价：扩展是
  **pre-v1（会破兼容）**、无 ANN 索引（暴力扫描）、LangChain 适配器在**未维护**的 `langchain-community`。
- B. **Chroma**（`langchain-chroma` 一手、活跃）：接口与过滤最好用；代价：**向量索引常驻 RAM**（硬内存开销）、
  引一个新的 `chromadb` 依赖树、与 SQLite 是两套存储。
- C. **pgvector**（`langchain-postgres`）：功能最全（ACID、声明式 filter、JOIN）；代价：
  **要起一个 Postgres 进程**，与「单机个人助手、尽量零外部服务」的现状冲突最大。
- D. **LanceDB / FAISS**：零服务，但适配器都在未维护包里；FAISS 还要自管持久化与元数据。

**决策 2：检索的触发形态——模型主动 vs 被动注入？**
- A. **工具（Agentic）**：零适配、上下文按需、与 MCP 工具同形；代价：模型可能不调或乱调，延迟不可预测。
- B. **中间件被动注入（2-Step）**：每次运行必检索，行为确定；代价：**每轮都吃检索延迟**、
  且要动中间件链（顶破两条既有测试断言，ADR-0018 §5）。
- C. **混合**：加一层「先判断要不要查」的节点；代价：多一次模型/规则判定，复杂度最高。

**决策 3：检索结果交给模型的形状——直接把正文塞回上下文，还是落 `/workspace` 给路径？**
- A. **直接返回正文**：简单、一步到位；代价：**每轮上下文都被 chunk 正文占满**，与压缩/预算冲突。
- B. **写 `/workspace` + 返回路径**（官方 Deep Agents 做法，§6.2）：上下文预算友好、与既有沙箱/子 agent 天然吻合；
  代价：多一轮读文件（或派子 agent），实现更重。

**决策 4：embedding 走在线 API 还是本地模型？**
- A. **在线 API**（OpenAI / DashScope-Qwen 等）：质量稳、无本地算力；代价：**每 chunk 都要出网 + 计价 + 数据外流**，
  用户需自取 key，且**索引动作会持续消耗额度**。
- B. **本地模型**（`bge-m3` / `Qwen3-Embedding-0.6B` / `bge-*-zh`）：零边际成本、数据不出本机；
  代价：拉 `torch`/`sentence-transformers`（或 `FlagEmbedding`）依赖、CPU 慢、要下模型权重（数百 MB~数 GB）。

**决策 5：是否上混合检索（向量 + BM25/全文）与重排？**
- A. **只做纯向量**：实现最省；代价：字面命中（专名/编号）会漏。
- B. **加混合**：要额外一份全文索引（sqlite FTS5 / Chroma FTS / LanceDB FTS）+ 融合（RRF）；
  代价：索引与融合逻辑都要自己搭（`EnsembleRetriever` 在 `langchain-classic`）。
- C. **加重排**：精度提升最确定（Anthropic 口径最大一档）；代价：**每次检索 +1 次 cross-encoder 前向或一次 API 往返**，
  本地还要再拉一个 reranker 模型。

**决策 6：配置与来源分层——知识库旋钮落在哪，文档从哪来？**
- A. **照 skill**：仓库目录为唯一真相、启动期投影、删除即失效、改了重启；代价：大库启动慢。
- B. **进 `config.yaml`**：与我们的工具一致；代价：与「源目录」可能重复表达同一件事。
- C. **进 `extensions_config.json`**：适合「外部知识源是扩展」的语义；代价：与 MCP 那层「外部进程声明」混住。
- 以及：**用户上传（`/input`）与目录挂载两种来源是否都要**、**索引启动期建还是增量建**。

---

## 附：本文的一手来源清单（按出现顺序）

- LangChain v1 迁移指南 — https://docs.langchain.com/oss/python/migrate/langchain-v1
- LangChain Retrieval 总览（2-Step / Agentic / Hybrid）— https://docs.langchain.com/oss/deepagents/retrieval
- LangChain Semantic search / knowledge base — https://docs.langchain.com/oss/langchain/knowledge-base
- LangChain Deep Agents RAG（retrieve/offload/delegate）— https://docs.langchain.com/oss/deepagents/rag
- LangChain Middleware overview — https://docs.langchain.com/oss/langchain/middleware/overview
- LangChain Context engineering — https://docs.langchain.com/oss/langchain/context-engineering
- LangChain Text splitters — https://docs.langchain.com/oss/integrations/splitters
- LangChain MarkdownHeaderTextSplitter — https://docs.langchain.com/oss/python/integrations/splitters/markdown_header_metadata_splitter
- langchain_text_splitters 源码（默认 separators / Language 枚举）— https://github.com/langchain-ai/langchain/blob/master/libs/text-splitters/langchain_text_splitters/character.py · https://github.com/langchain-ai/langchain/blob/master/libs/text-splitters/langchain_text_splitters/base.py
- LangChain Vector store integrations — https://docs.langchain.com/oss/integrations/vectorstores
- LangChain Chroma 集成 — https://docs.langchain.com/oss/python/integrations/vectorstores/chroma
- LangChain PGVector 集成 — https://docs.langchain.com/oss/python/integrations/vectorstores/pgvector
- LangChain Embedding integrations — https://docs.langchain.com/oss/integrations/embeddings
- langchain-community retrievers（含 BM25Retriever）— https://github.com/langchain-ai/langchain-community/blob/main/libs/community/langchain_community/retrievers/bm25.py
- langchain-community SQLiteVec — https://github.com/langchain-ai/langchain-community/blob/main/libs/community/langchain_community/vectorstores/sqlitevec.py
- `langchain-classic`（PyPI）— https://pypi.org/project/langchain-classic/
- `langchain-text-splitters`（PyPI）— https://pypi.org/project/langchain-text-splitters/
- `langchain-huggingface`（PyPI）— https://pypi.org/project/langchain-huggingface/
- sqlite-vec README — https://github.com/asg017/sqlite-vec
- sqlite-vec Python binding — https://alexgarcia.xyz/sqlite-vec/python.html
- sqlite-vec vec0 虚拟表 — https://alexgarcia.xyz/sqlite-vec/features/vec0.html
- sqlite-vec KNN — https://alexgarcia.xyz/sqlite-vec/features/knn.html
- LanceDB README — https://github.com/lancedb/lancedb
- LanceDB 增量重索引 — https://docs.lancedb.com/indexing/reindexing
- Chroma 架构总览 — https://docs.trychroma.com/reference/architecture/overview
- Chroma 单机性能 — https://docs.trychroma.com/guides/performance/single-node
- Chroma Clients — https://docs.trychroma.com/docs/run-chroma/clients
- FAISS README — https://github.com/facebookresearch/faiss
- pgvector README — https://github.com/pgvector/pgvector
- BGE-M3 模型卡 — https://huggingface.co/BAAI/bge-m3
- BGE-M3 / M3-Embedding 论文 — https://arxiv.org/abs/2402.03216
- bge-large-zh-v1.5 模型卡 — https://huggingface.co/BAAI/bge-large-zh-v1.5
- bge-reranker-v2-m3 模型卡 — https://huggingface.co/BAAI/bge-reranker-v2-m3
- bge-small-zh-v1.5 模型卡 — https://huggingface.co/BAAI/bge-small-zh-v1.5
- Qwen3-Embedding-0.6B 模型卡 — https://huggingface.co/Qwen/Qwen3-Embedding-0.6B
- Qwen3-Embedding-8B 模型卡（含 C-MTEB 表）— https://huggingface.co/Qwen/Qwen3-Embedding-8B
- multilingual-e5-large 模型卡 — https://huggingface.co/intfloat/multilingual-e5-large
- OpenAI embeddings 指南 — https://developers.openai.com/api/docs/guides/embeddings
- OpenAI pricing — https://developers.openai.com/api/docs/pricing
- DashScope 文本嵌入同步 API — https://www.alibabacloud.com/help/en/model-studio/text-embedding-synchronous-api
- Jina embeddings v5 模型卡 — https://huggingface.co/jinaai/jina-embeddings-v5-text-small
- Jina Embeddings API — https://jina.ai/embeddings
- Cohere Embed 模型 — https://docs.cohere.com/docs/cohere-embed
- Cohere pricing — https://cohere.com/pricing
- Voyage text embeddings — https://docs.voyageai.com/docs/embeddings
- Voyage pricing — https://docs.voyageai.com/docs/pricing
- SiliconFlow 嵌入模型目录 — https://siliconflow.cn/models?types=embedding
- Anthropic Contextual Retrieval — https://www.anthropic.com/news/contextual-retrieval
- Cohere Rerank 文档 — https://docs.cohere.com/docs/rerank-overview
- MTEB leaderboard — https://huggingface.co/spaces/mteb/leaderboard
