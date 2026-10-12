# RAG 知识库：目录即真相 + 按文件增量重建 + 命中落 `/workspace`

三期清单（#13）的 Rag 项。选型经 2026-10-11 一轮研究定稿，底稿在仓库
（`docs/research/rag-knowledge-base.md` 文本全链、`docs/research/rag-multimodal.md` 多模态补篇，
后者本片**不用**）。本片只做**纯文本端到端**：图片、混合检索、重排都留后续。

参照项目 DeerFlow 在 RAG 上**零实现**，所以这份 ADR 没有可对照的先例，只有外部调研与实测。

## 决策

**1. 机制放 harness（`evaharness/fingerprint` + `evaharness/knowledge`），策略在 app。**

与沙箱同一条分界（ADR-0020）。harness 里是通用的机器：指纹、切分、向量库、索引器、检索工具；
app 里是部署决定：源目录在哪、库文件在哪、用哪个嵌入模型、切多大。**harness 的这两个包不 import
任何业务模块**——`fingerprint` 尤其干净，它连「索引」这个词都不认识，只把一个目录压成一份
可按文件比较的指纹。

**2. 变更检测独立成模块，粒度是**单个文件**，返回 `ChangeSet` 而不是 bool。**

`TreeFingerprint.entries` 是「相对路径 → 单文件指纹」，所以**增 / 删 / 改都能读出来**——整棵树的
摘要一变，你只知道「变了」，不知道是哪个文件，那对增量重建没用；**删除**更是只有按文件比才看得见。

`fingerprint(paths, ignore=(), mode="mtime"|"content")` 两种模式：`mtime`（`mtime_ns:size`，快）与
`content`（`sha256`，准）。不存在的路径**抛 `FileNotFoundError`**（「你指错地方了」与「这里什么都没有」
是两件事），空目录则是一份空 entries。

**3. 重建单位是文件——这是对 issue 原范围的刻意扩大。**

issue #19 原写「本片只做全量重建 or 跳过」「增量重建留后续」。**用户在本片明确要求按文件粒度重建**
（理由：整库重来太贵），所以纳入本片。落地方式是 `upsert_file` **先删该文件的全部旧 chunk、再插新的**
（chunk 的 id 是 ``f"{source}::{ordinal}"``、元数据里带 ``source``，所以「删掉某个文件的全部 chunk」
是一条 ``where`` 查询），于是「改 10 个文件里的 1 个」只重嵌那 1 个。
删除也在 `ChangeSet` 里，所以移走的文件不会留残影。

**4. 索引重建丢**后台任务**，服务不等它。**

源大时重建会慢，而启动被拖住是实打实的体验问题。所以 lifespan 只做「建库、建检索器与工具、起一个
后台 task」就 `yield`。代价是重启后第一声检索可能落在索引还没建好的时候——用一道**就绪闸门**处理：
`KnowledgeRetriever.retrieve` 先 `await` 就绪信号；构建**失败**则抛 `KnowledgeUnavailable`
（**可见地报错**，不是静默返回空——空结果与「还没建好」在模型眼里是一回事，那是本项目一贯拒绝的
失败形状）。

**5. 向量库是 ChromaDB（嵌入式持久化），自己写薄适配。**

它有一手维护的适配器、接口与元数据过滤最好用，嵌入式运行（一个目录、零外部服务）。
**这条推翻了研究文档与 issue #19 的初选**——初选是 sqlite-vec（唯一「零新增基础设施」，代价是
**pre-v1、无 ANN 索引**、适配器在已停维护的 `langchain-community`）。用户在本片定稿后要求改用它，
理由成立：pre-v1 的破兼容风险是长期负担。代价照实记：**依赖树不轻**（chromadb 带进
onnxruntime / tokenizers / numpy / opentelemetry 一串）、**向量索引常驻内存**（几百 MB~几 GB）、
**多了一套存储**（Chroma 自己用 SQLite 存文档与元数据，与我们 `data/` 下的 checkpoint 不是同一个库）。

Chroma 的客户端是**同步**的，所以每个调用都丢工作线程（`asyncio.to_thread`）；真正慢的嵌入是
异步的。

**6. 中文分隔符必须显式传，且 `keep_separator="end"`。**

`RecursiveCharacterTextSplitter` 的默认分隔符是纯英文 / 空白导向，**没有任何中文标点**，而
`from_language(...)` 的 `Language` 枚举是**编程语言**不是自然语言——所以中文不传就只能在字符层面硬切。
显式传 `["\n\n", "\n", "。", "！", "？", "；", "，", "、", " ", ""]`（官方 recipe 漏了 `！？；`）。

**实测踩到的坑**：分隔符默认行为 `keep_separator=True` 把标点留在**下一片的开头**（一片以「。」起头），
既难看又让嵌入多读一个孤立标点。`keep_separator="end"` 把它留在前一片末尾——句子以句号收尾、标点不丢。

**7. 嵌入走 DashScope 的 OpenAI 兼容端点。**

`OpenAIEmbeddings(base_url=https://dashscope.aliyuncs.com/compatible-mode/v1)`，不必为它引新 SDK。
两个实测参数：`batch_size=10`（v4 每次最多 10 条），`check_embedding_ctx_length=False`（默认那条会拿
tiktoken 估长度，而 v4 不是它认识的模型；长度我们自己按字符控制在切分层已管住）。

**8. 检索工具把命中落 `/workspace`、只回路径。**

官方 Deep Agents 的「retrieve, offload, and delegate」形态。好处是**上下文预算按需**：不检索零成本，
检索了也只回几行路径，正文从不无条件顶进上下文。落 `/workspace` 而不是 `/output`——后者是交付、
`present_files` 只放行它；检索命中的中间材料是模型自己要看的东西。目录名取 query 的哈希，所以工具
**无状态**（跨请求共享，ADR-0011）。

**9. `/knowledge` 是第五个全局只读挂载，启动期从仓库 `knowledge/` 镜像。**

照 `/skills`（ADR-0018）的做法：源是仓库根 `knowledge/`（进版本控制、可 review），视图是
`backend/.evaai/knowledge-view/`（运行期数据、已 gitignore），挂载表里
`Mount("/knowledge", writable=False, parts=("knowledge-view",))`。有了它，模型命中一个片段后还能
`read_file /knowledge/xx.md` 读**原件全文**——检索只给 chunk，原件得另有出处。

**源目录不存在时不创建视图**（如实报「不存在」比造个空目录好）；投影是**镜像**，源里删掉的必须从视图
一并消失。

**10. 工具是启动期常量，与 MCP 工具并列进 `AgentFactory.tools`——不进 `tools:` 注册表。**

这与 `web_search` 那种「`use:` 指向工厂」的形状**刻意不同**：索引重建天生要一个**异步启动期钩子**，
而 `tools:` 的工厂是同步构造、拿到的只有自己那几个扁平额外键。所以知识库有一个自己的
`knowledge:` 配置节，lifespan 读它、建出工具、`append` 进工具表——与 MCP 工具是同一条路（启动期常量、
运行期零 I/O、主图子图自动都有）。代价是工具来源多了一个；但 `enabled` 是显式开关，不是隐藏默认集
（ADR-0012 那条「工具是能力边界、没有隐藏默认集」不破）。

**11. `enabled` 默认关；关了不加工具、不建库、不报错。**

没有嵌入 key 时，一个可选功能不该让服务起不来。而 `enabled=true` 却缺 `api_key` 时**拒绝启动**——那
已经是显式配置错，不是「功能没开」。

**12. 提示词里那段知识库说明是**条件拼接**的。**

`KNOWLEDGE_SECTION` 只在检索工具真的在表里时才拼进 `system_prompt`（与 bash 段同一处、同一做法，
ADR-0019 §12）。它顺带是「`/knowledge` 这个第五个只读目录存不存在」的告知。既有的「空时逐字节原样
返回」契约不变——`SYSTEM_PROMPT` 里那句「四个虚拟目录」改成了「这些」，免得写死数目。

**13. 索引配方（recipe）进向量库，一变就整库重建。**

比对源目录指纹**不足以**发现「换了嵌入模型」——源一个字没改，可已入库的向量其实全部作废了：换成
语义空间不同的同维模型 → 检索结果**静默变差**；换成不同维度 → 写入**直接报错**。两种都很难查。

所以把「这份索引是怎么算出来的」——嵌入模型 / 端点 / 维度 / `chunk_size` / `chunk_overlap` /
**切分配方版本号**（:data:`~evaharness.knowledge.SPLITTER_RECIPE`，改了切分行为就加一）——存进
Chroma 集合的元数据，启动时 :meth:`~evaharness.knowledge.store.KnowledgeStore.ensure_recipe` 比对：
不一致就**丢弃整个集合重建**（连维度设定一起重来），并忽略源指纹差异、全量重嵌。

切分逻辑也编进版本号，是因为它一改，同一份源就会切出不同的片段——那是「配方变了」，同样该整库重建。

## 硬约束

- **指纹的 SQLite 存储不跨线程用**。实测踩到：`start_knowledge` 在事件循环线程建连接，重建任务里
  若用 `asyncio.to_thread` 去 `save` 就会撞 `sqlite3.ProgrammingError`（SQLite 的线程亲和性检查）。
  所以**扫盘（`fingerprint`）与嵌入（网络）才丢线程 / 走异步，指纹的读写直接在循环线程上做**
  （毫秒级，不值得换线程）。
- **工具实例跨请求共享**（ADR-0011），所以检索器与工具都不持有请求级状态：写哪个目录由运行上下文里的
  会话 id 决定，目录名由 query 哈希决定。
- **Chroma 的索引常驻内存**，且它是**同步**客户端——调用一律丢工作线程，别占着事件循环。
- **向量库是多出来的一套存储**：Chroma 自己用 SQLite 存文档与元数据，与我们 `data/` 下的
  checkpoint 不是同一个库，不要指望它们能在一个事务里。
- **嵌入是每 chunk 一次出网 + 计价**；索引动作会持续消耗额度。所以只嵌**变动过的**文件这件事不只是
  性能，也是成本。

## 后果

- **`DEFAULT_MOUNTS` 从四条变五条**（多 `/knowledge`）：容器多一个 `:ro` 挂载、`mount_list_text`
  多一项、`test_sandbox` 那条「每个挂载点都可达」的断言跟着改。
- **harness 依赖 +2**：`langchain-text-splitters`（切分）与 `chromadb`（向量库，依赖树不轻）。
- **运行期多两处数据**：`data/knowledge/`（Chroma 的持久化**目录**）与 `data/fingerprints.sqlite`
  （指纹），都在已 gitignore 的 `data/` 下。
- **`DASHSCOPE_API_KEY` 在 `.env` 里已经就位**，issue 里那条「阻塞项」在本机不是障碍。
- **本片只接 RAG 一个消费者**：`fingerprint` 模块的形状已经为 config / skill 的变更检测留好了缝，
  但它们的接入不在本片验收里。

## 不做 / 范围外

图片 / 多模态、混合检索（向量 + BM25）、重排、常驻 fs 事件监听（「监听」= 启动期快照比对）、
运行时上传、多用户权限、config 与 skill 的消费者接入、镜像/replication。

## 与参照项目的对照

| 处 | DeerFlow | EvaAI |
|---|---|---|
| RAG | **零实现** | 本片从零建（无先例可对齐） |
| 检索结果交给模型的形状 | — | 落 `/workspace` + 只回路径（对齐 LangChain Deep Agents 的 retrieve-offload） |
| 部署策略怎么进检索 | — | `knowledge:` 配置节 → lifespan 建启动期常量（对齐自己的 MCP / 沙箱） |
| 向量库 | — | ChromaDB（嵌入式；代价是依赖树重 + 索引常驻内存） |
