# EvaAI

企业级可用标准的个人 AI 助手。本文件是 EvaAI 的**词汇表**：只定义本项目的语言，不含实现细节与设计决策（那些在 `docs/adr/`）。

## 语言

### 架构分层

**harness（框架层）**：
EvaAI 中业务无关、只提供可插拔机制的那一层。分界的一句话是：**机制在 harness，策略在 app**。
沙箱、文件工具、容器、事件契约、技能解析、MCP 装配都在这一层；而「装哪些工具、宿主根在哪儿、
挂载表长什么样、人设是什么」在 app。
_Avoid_: framework, 核心库, core, 基础设施

**app（业务层）**：
EvaAI 中装填具体实例（人设、工具、技能）的那一层。**工具的注册表（`config.yaml` 的 `tools:`）
在这一层**——装哪些工具、叫什么名字、开放哪些参数是业务决定（ADR-0012）；但工具的**实现**
从 T17 起在 harness（ADR-0020），两者别混。
_Avoid_: application, 业务代码, server

**runtime（运行层）**：
harness 里承接「跑一次 agent」的那个包：事件的契约、这一轮的运行身份、以及跑它的机制。
它是 harness 的**契约中枢**——沙箱、知识库、journal、模型这些机制都依赖它，而它谁也不依赖
（不 import `agents`、不 import 任何工具包），这条方向由 `tests/test_harness_boundary.py` 的一条
守卫测试钉死。内部按 `event` / `context` / `runs` 三个子包分，但**对外只认
`evaharness.runtime` 这一条路径**——子包是给人导航用的，不是 API 面。
_Avoid_: 运行时（那指代码跑起来的那个环境，是另一回事）、执行层、runtime 模块

### 执行模型

**ReAct**：
一个「推理 → 行动 → 观察」的循环：模型自己决定调用哪个工具、调几次、何时收尾。
EvaAI 里所有 agent（主 Agent、子 agent）都是 ReAct 图。
_Avoid_: react agent, 循环体, agent loop

**lead agent（主 Agent）**：
项目**默认**的顶层 agent——一个 ReAct 图，用户直接与它对话。多轮记忆、会话摘要压缩、
工具调用都挂在它身上；它就是用户眼里那个「EvaAI」。
_Avoid_: orchestrator, supervisor, 总控, main agent, leader agent

**Plan-Exec**：
把用户请求拆成任务图、派发给执行单元、再汇总结果的编排模型。**T8 起不再是默认路径，T16 起
整条下线**（见 ADR-0021）——代码、路由、测试都删了。这个词现在只出现在**历史语境**里：
T8 之前落盘的 journal 行里确实有 `plan` / `task_start` / `summary` 这些事件，前端回放老会话时
仍会渲染它们。**新运行不会再产生任何一条。**
_Avoid_: plan-and-execute, plan-and-solve, 规划执行

**planner**：
Plan-Exec 里产出任务图的角色。随 Plan-Exec 在 T16 下线（ADR-0021）。
_Avoid_: 规划器, 调度器, 拆解器

**replanner**：
Plan-Exec 里每批任务结束后判断「继续还是结束」的角色（不重写计划）。随 Plan-Exec 在 T16 下线。
_Avoid_: 重规划器, 动态规划器

**任务执行单元**：
Plan-Exec 里承载单个任务的 ReAct 图。随 Plan-Exec 在 T16 下线。
_Avoid_: worker, ReAct 执行器

**思考档位（thinking level）**：
一次运行的思考深度，四选一：`off`（界面上叫「极速」）、`low`、`high`、`max`。它随每次请求下发、
随 `run_start` 事件回显，所以回放时前端知道历史那一轮用的是哪一档。
_Avoid_: reasoning_effort（那是**发给 API 的字段名**，不是我们这个概念的叫法）、思考模式（那只说了
开没开、没说多深，而这两件事在本项目里是**一个**四选一）

**思考内容（reasoning）**：
模型给出的推理过程（DeepSeek 的 `reasoning_content`），与正文**是两条并行的流**，在界面上
**自成一个步骤**（「第 N 步 · 思考」），与「第 N 步 · 调用 X」共用同一套折叠与实时打字。
它属于「过程」，**不是**模型对用户说的话。
_Avoid_: 思维链、CoT、推理（这三个都太宽，指不清是「模型想的」还是「它答复的」）

**subagent（子 agent）**：
由主 Agent 通过 `task` 工具派发出去、独立完成**一件事**的 ReAct 图。它看不到父对话，
中间步骤也不会回流到主对话——**上下文隔离才是它存在的理由**，不是「更快」。
_Avoid_: worker, 任务 agent, child agent

**运行入口（`run_agent`）**：
跑一轮 agent、把图的输出翻成事件流的那**一个**函数。它只吃一张编译好的图与一串参数，
不认识「lead」是谁——泳道（`lane`）与路径（`mode`）都是参数，缺省值对着主 Agent。
事件序列以 `run_start` 开场、以终态事件收尾。
_Avoid_: runner、执行器、agent loop、驱动（那是「谁在调它」的说法）

**运行上下文（`RunContext`）**：
一次运行在图里对中间件与工具**自称**的样子：会话 id、检查点坐标、运行 id，以及这一轮要用的
沙箱布局。工具与中间件靠它知道「此刻是哪次运行」——**不是**靠进程全局，也不是靠读配置。
_Avoid_: context（太宽）、会话（那是它三个字段之一）、runtime context（与上游 langgraph 的
`Runtime` 撞名）

### 装配

**建图工厂（AgentFactory）**：
按**请求**构建图与角色的唯一入口——主图、子 agent 图、模型，以及封存路径的
主图与子图。它只持有跨请求**必须**共享的建图参数（配置、业务工具、
checkpointer），自己不持有任何图或模型。
_Avoid_: agent 池, 图缓存, builder, 单例

**每请求建图（per-request build）**：
图与模型的寿命 = 一次请求 = 一条响应流。中间件的追踪状态因此不跨请求；**记忆不受影响**
（记忆在 checkpointer 里，不在图里）。代价是请求期建图失败表现为 500 而不是 SSE 错误帧。
**不做缓存**——缓存会把跨请求共享的中间件实例原样带回来。
_Avoid_: 无状态图, rebuild, 重建, 热加载

**启动期 smoke build（冒烟装配）**：
启动时把图装配一遍再丢弃，只为把「装配连线断了」这类错误提前到起服务时，失败即拒绝启动。
它**抓不到**工具重名、`state_schema` / `context_schema` 传错——不是正确性证明。
_Avoid_: 自检, health check, 预热（warmup 会保留产物，这里不保留）

### 工具与文件

**工具注册表（tool registry）**：
**我们自己的工具**（业务工具）的唯一来源：`config.yaml` 里列出工具（`name` + `use`），启动期反射
建出 `BaseTool`。`use` 可以指向工具**实例**，也可以指向**工厂**——工厂的额外键就是它的构造参数
（参数在构建期绑进闭包，工具实例因此是自足的）。写错就在启动期拒绝启动。
T13 起 agent 的工具表还有**第二个来源**：MCP（见下），它不在 `tools:` 里、也不在注册表里。
_Avoid_: tool loader, 插件系统, 工具工厂

**挂载根 / 虚拟路径（mount root / virtual path）**：
模型看到的目录名（`/workspace`、`/output`、`/input`、`/skills`、`/knowledge`）与它们对应的
宿主目录之间的映射。**宿主绝对路径永远不进模型上下文**（泄漏 + 不可移植）。这些挂载点曾经是
「将来换真沙箱」的唯一缝——T15 换了（见「沙箱容器」），而缝的形状没变：**同一个虚拟路径在两边
都是同一个路径**。这张表本身（名字 + 读写权限）由**沙箱布局**带着走，框架给默认五条、部署可覆盖。
_Avoid_: 沙箱路径, root path, 别名目录

**沙箱容器（sandbox container）**：
`bash` 工具执行命令的那台 Linux 容器。**每会话一个、长驻**，名字 `evaai-{会话}`，所以它**不是**
「每次调用一个」，也**不是**「会话」——它是会话的运行状态（删会话连带删它）。
它的四个挂载点就是虚拟路径本身，于是容器里写 `/workspace/x` 与文件工具的 `/workspace/x` 是
**同一个文件**，不是「两套坐标系靠约定对齐」；`/input` 与 `/skills` 的只读在这里由 docker 强制。
四个目录**之外**的东西（装的包、`/tmp`、后台进程）文件工具看不见、容器重建就没——所以「要留下的
写 `/workspace` 或 `/output`」是模型必须知道的一条。
它挂在哪个宿主根、用哪张挂载表，由**沙箱布局**决定。
_Avoid_: 沙箱（在本项目里那指虚拟路径那一层机制）、docker 环境, 执行环境, 沙箱会话

**沙箱布局（sandbox layout）**：
一次运行注入的「**宿主根 + 挂载表 + 相对路径的落点**」，类型是 `SandboxLayout`。它取代了
T17 之前的模块全局 `SANDBOX_ROOT`：宿主根与挂载表是**部署策略**，由 app 构造一次、经
**运行上下文**（`RunContext` 的 `sandbox` 字段）下发给工具——与 `session_id` 同一个口子。
所以框架里没有任何全局决定「文件落在磁盘的哪里」，而测试换根的方式是「造一个指向 `tmp_path`
的布局」，不是 monkeypatch 全局。**取不到就抛**（与 `session_id` 同口径）：兜到某个猜出来的
目录会把文件写到不该写的地方，而且不可逆。
_Avoid_: 沙箱配置（那是给人看的旋钮，这是运行期注入的对象）、根路径、SANDBOX_ROOT（旧的全局名）

**工作台（workspace）**：
会话级的工作目录，相对路径的落点，草稿与中间产物放这里。与「交付」（`/output`）和「用户上传」
（`/input`）分开，是为了让原件与成品都不被中间过程污染。
_Avoid_: tmp, 临时目录, sandbox

**只读挂载（read-only mount）**：
`/input`、`/skills`、`/knowledge` 的权限。`/input` 因为改坏原件不可逆，`/skills` 因为可被模型
改写的技能等于把提示注入面交给它，`/knowledge` 因为它是知识库的原文（源是仓库里的 `knowledge/`，
视图只是它的镜像，改了也没意义）。T14 起 `/skills` 有了生产者——**启动期**把仓库里的技能源目录
镜像过来（:func:`app.gateway.skills.project_skills`）；T18 起 `/knowledge` 有了生产者
（:func:`app.tools.knowledge.project_knowledge`，同一条路）。所以它们既不按会话分，也不由谁在
会话里创建。
_Avoid_: 权限, ACL

**交付文件（presented file）**：
模型通过 `present_files` 工具**显式**交给用户的成品。它必须是 `/output` 下的文件，是一条
`files_presented` 事件，前端渲染成可下载的卡片、也随 RunJournal 落盘（刷新后卡片仍在）。
「显式」是要点：`/workspace` 里的东西**不算**交付文件，写得再多也不会出现在用户面前。
_Avoid_: artifact, 产出物, 附件, 文件引用（DeerFlow 管这个叫 artifact，那是它的 state channel 概念）

**MCP 连接（MCP connection）**：
客户端与某个 MCP server 之间那条连接。**生命周期 = 一次连接用途**：启动期那一次是为了**取工具
列表**（取完就断），之后每条都建在模型**真的调某个工具**的那一刻（ADR-0017 补记二）。
所以运行期**没有任何按运行的 MCP 资源**——工具清单是启动期常量，运行期零 I/O。
它**不是**「会话」——「会话」在本项目里特指用户眼里那段连续对话。
_Avoid_: session（上游 API 的叫法，会与 thread（会话）撞车）、MCP 会话、把连接说成「每请求的」

**MCP 工具名**：
`mcp__<server>__<tool>`，双下划线分隔。**始终加**，不是「撞名才加」——工具名一旦随配置变化，模型在
对话里学过的名字与 RunJournal 里记的调用行就都失效了（journal 是要能回放的老数据）。
**前缀同时就是挡撞名的那道墙**：server 提供 `read_file` 时它变成 `mcp__fs__read_file`，碰不到我们的
`read_file`，也**永远不可能等于** `task` 这类裸的保留名。
_Avoid_: `<server>_<tool>`（那是库默认在多 server 模式下的形状，分不清 server 名到哪结束）

**技能（skill）**：
仓库 `skills/<名>/SKILL.md` 里的一段「做事方法」，启动期**镜像**到 `/skills` 只读视图，模型需要时
**自己**用 `read_file` 读全文。提示词里只放它的 `name` / `description` / 位置——**正文永远不进提示词**
（保持提示词静态、前缀缓存友好）。技能名以**目录名**为准，frontmatter 的 `name` 必须与它一致。
_Avoid_: 提示词模板、插件、工具（技能不调工具，它告诉模型**怎么做**）

**技能清单（skill section）**：
`render_skills_section` 渲染出来的那一段，拼在 `system_prompt` 后面。**没有技能时它整段消失**——
拼装代码不能顺手加一个换行，否则没有技能的部署会拿到一份不同的提示词。
_Avoid_: 技能列表、技能索引（那是参照项目的 `skill_index`，指「只给名字」的那条渐进披露路径）

### 知识库

**知识库（knowledge base）**：
仓库根 `knowledge/` 下的文本，启动期切分、嵌入、存进本地 ChromaDB 向量库，模型再用
「检索工具」按需取用。**源目录即真相**，启动期镜像到只读的 `/knowledge`；索引按**文件**增量重建。
它**不是**「会话记忆」——那是 checkpoint 里的事，与知识库是两条线。
_Avoid_: RAG（那是一门技术的名字，不是这个实体）、向量库、语料库、记忆

**索引配方（index recipe）**：
这份索引是「**怎么算出来的**」——嵌入模型 / 端点 / 维度 / 切分参数 / 切分配方版本号。它记在向量库
里，启动时比对：不一致就**整库重建**。有了它，换嵌入模型不会留下「源没变、向量却作废」的静默错误
（症状是检索变差或维度对不上直接报错）。
_Avoid_: 配置指纹、索引版本、schema 版本

**检索工具（retrieval tool）**：
知识库对模型暴露的**唯一**入口，工具名 `knowledge_search`。它**不把正文塞回上下文**：命中的片段
写进 `/workspace/retrieved/<query 哈希>/chunk_i.md`，只回路径，模型自己 `read_file` 去读（官方
Deep Agents 的 retrieve-offload 形态）。这是「上下文预算按需」那条约定在 RAG 上的落点。
_Avoid_: 知识库工具、搜索工具、retriever（那是它内部用的向量检索器）

**指纹（fingerprint）/ 变更集（ChangeSet）**：
指纹是一组路径压成的「相对路径 → 单文件指纹」快照（`TreeFingerprint`），**粒度到单个文件**；
变更集是两份指纹的差异，分 `added` / `removed` / `modified` 三个集合。它**只回答「变了没、变哪些」**，
不决定任何动作。放在 harness（`evaharness/fingerprint`）、**不 import 任何业务模块**——知识库是它
的第一个消费者，config / 技能的接入是后续。
_Avoid_: 哈希、摘要、checksum、脏标记

**按文件增量重建**：
知识库索引的重建单位是**单个源文件**而非整库：只有 `ChangeSet` 里变动的文件被重切、重嵌、替换，
删掉的文件随之清出。所以「改了一个文件」的代价是重嵌那一个文件。
_Avoid_: 增量索引、增量同步、差分更新

**就绪闸门（readiness gate）**：
`KnowledgeRetriever` 上的一道等待：索引重建在**后台任务**里跑（服务不等它），检索前先等就绪信号。
构建**失败**时检索**可见地报错**（`KnowledgeUnavailable`），而不是静默返回空结果。
_Avoid_: 预热、加载中标记、健康检查

### 长期记忆

**长期记忆（long-term memory）**：
**跨会话**留在本地的两样东西：**画像**（六块叙述性摘要）与**事实**（一条条原子断语），存在
`data/memory.sqlite` 里。**它不是会话记忆**——那是 checkpointer 里那条 `{会话}:lead` 坐标，
随会话生灭；也不是知识库——那是仓库文本的向量索引，是「查资料」。它与 `SummarizationMiddleware`
更不重叠：那个压的是**本 thread** 的上下文，画像里的 `history` 三块是跨会话的长期历史记录。
_Avoid_: memory（太宽，指不清哪一层）、聊天记录、用户档案（那是它的一部分）、RAG 记忆

**画像（profile）/ 事实（fact）**：
画像分两组共六块：`user`（`work_context` / `personal_context` / `top_of_mind`）与 `history`
（`recent_months` / `earlier_context` / `long_term_background`），每块一段自由文本；事实是原子
条目，带九选一的 `category`（其中 `correction` 享注入的保底通道）、`confidence` 与来源会话。
**事实按内容去重**（唯一索引），**画像按块覆盖**。两者都由 T21 的抽取器生产。
_Avoid_: 记忆条目、知识点、条目化记忆

**记忆注入（memory injection）**：
一个会话**开头**把当前记忆渲染成一段 `<memory>…</memory>`，作为一条 `role=user` 的**隐藏消息**
（`hide_from_ui`）**写进 thread state**，此后不再重复。模型每轮都能从历史里看到它，而它不进
journal、不出现在前端对话里（事件翻译层按 `hide_from_ui` 兜底过滤）。**它不是每轮回放**——
T20 曾在 `wrap_model_call` 里按请求 override，每个 ReAct 步骤都会重复一遍，T21 改掉了。
_Avoid_: 记忆提示词、上下文拼接、注入消息（那是一次性的载体，不是一个概念）、每轮注入

**记忆抽取（memory extraction）**：
一轮对话结束后，采集中间件把「真实用户消息 + 最终助手回复」丢进按会话去抖的后台队列
（默认 30 秒），由抽取模型整理成画像更新与事实增删改，成功后推进**水位线**。水位线是最后一条
已抽取 AI 消息的 id——它是**身份**不是游标，用来在 state 里定位「从哪之后还没抽」。抽取失败
不推进水位线，下次连同失败那轮一起重抽；关机时未决项直接丢弃（下次会补上）。
_Avoid_: 记忆整理、后台任务、信号检测（我们不做 DeerFlow 那套规则打分）

**记忆工具（memory tools）**：
代码里有三件：`memory_save` / `memory_search` / `memory_forget`；**模型只拿 `memory_search`**
（T21 起写入是被动的，save / forget 只留给管理页与将来的 tool 模式）。`memory_search` 支持
`since`（ISO 8601，按 `updated_at` 过滤）。它们都**只属于主 Agent**（与 `present_files` 同规则：
子 agent 上下文隔离），且都是 `async def`——同步工具会被 langgraph 丢进工作线程，而存储的
SQLite 连接有线程亲和性。
_Avoid_: 记忆 API、记忆接口

### 交付

**垂直切片（tracer bullet）**：
一条端到端打穿各层、自身即可演示或验证的交付单元。
_Avoid_: milestone, 里程碑, 阶段, horizontal slice

**T1–T20**：
当前垂直切片的编号。**T 号与 GitHub Issue 号不一一对应**（T3 是 #4、T4 是 #3、T7 是 #7、T13 是 #14、T14 是 #15、T15 是 #16、T16 是 #17、T17 是 #18、T18 是 #19、T19 是 #20、T20 是 #21），引用时写全「T3（#4）」以免混淆。T7 起都是原 MVP（T1–T6）之外新增的：T7 见 [Issue #7](https://github.com/liuzhenfang001/EvaAI/issues/7)，T8（换成 ReAct 主 Agent）见 [Issue #8](https://github.com/liuzhenfang001/EvaAI/issues/8)，T9（图改为每请求构建）见 [Issue #9](https://github.com/liuzhenfang001/EvaAI/issues/9)，T10（回复渲染 Markdown）见 [Issue #10](https://github.com/liuzhenfang001/EvaAI/issues/10)，T11（产出物以文件卡片展示）见 [Issue #11](https://github.com/liuzhenfang001/EvaAI/issues/11)，T12（思考模式与动态深度）见 [Issue #12](https://github.com/liuzhenfang001/EvaAI/issues/12)，T13（MCP 集成）见 [Issue #14](https://github.com/liuzhenfang001/EvaAI/issues/14)，T14（skill 加载机制）见 [Issue #15](https://github.com/liuzhenfang001/EvaAI/issues/15)，T15（docker 沙箱）见 [Issue #16](https://github.com/liuzhenfang001/EvaAI/issues/16)，T16（Plan-Exec 下线）见 [Issue #17](https://github.com/liuzhenfang001/EvaAI/issues/17)，T17（沙箱上移 harness）见 [Issue #18](https://github.com/liuzhenfang001/EvaAI/issues/18)，T18（RAG 知识库）见 [Issue #19](https://github.com/liuzhenfang001/EvaAI/issues/19)。T19（运行期收进 `runtime`）见 [Issue #20](https://github.com/liuzhenfang001/EvaAI/issues/20)。T20（长期记忆：存储 + 注入 + 主动工具 + 管理页）见 [Issue #21](https://github.com/liuzhenfang001/EvaAI/issues/21)，T21（长期记忆二期：被动抽取 + 会话开头注入）见 [Issue #22](https://github.com/liuzhenfang001/EvaAI/issues/22)。二期到 T12 收尾，T13 起是[三期](https://github.com/liuzhenfang001/EvaAI/issues/13)。
_Avoid_: M2/M3/M4（旧的水平里程碑分法，已废弃）

### 可观测

**thread（会话）**：
一段连续的对话，由多次运行组成。它不是一张表里的实体，而是 RunJournal 按 `thread_id` 的分组——会话由第一次运行落盘时自然形成。
_Avoid_: session, conversation, 聊天记录

**checkpoint thread / 检查点坐标**：
某张 ReAct 图的状态在 checkpointer 里的 key。与「会话」同名但不同层——它标的是
「哪张图」。三种，全部以 `{会话}:` 开头，所以删会话时一次前缀扫描就能清干净：

| 谁 | 形状 | 生命周期 |
|---|---|---|
| 主 Agent | `{会话}:lead` | **会话级、跨轮固定**——多轮记忆的来源 |
| 子 agent | `{会话}:{运行}:sub:{调用}` | 运行级，跨轮不累积 |
| Plan-Exec 任务 | `{会话}:{运行}:{任务}` | 运行级（**已下线**；老 checkpoint 里还有） |

它也是 LoopDetection 的分桶键，并出现在 RunEvent 的 `metadata.thread_id` 里
（那是 LangGraph 自己的键，我们原样保留）。**账本本身活在每请求一份的中间件实例里**，
随请求消失——所以坐标跨轮复用（`{会话}:lead` 就是）不会让账本跨轮累积。
_Avoid_: 与「thread（会话）」混用；读 `metadata.thread_id` 时误以为它是会话 id

**lane（泳道）**：
RunEvent 的 `task_id` 在界面上的分组。主 Agent 用约定的 `main`；`t1`/`t2` 是已下线的 Plan-Exec 留下的，只在回放老会话时见到。
它**跨运行会重复**（同一个会话的第二轮又会出现 `t1`），所以只能当展示标签、不能当身份；
气泡身份是 `runId:lane`。
_Avoid_: 用 lane 当消息 id；把它当成任务的持久标识

**轮次（round）**：
模型的一次发言：一段文本 + 它在这一轮发起的工具调用。边界由**消息语义**决定——收到工具结果
即关闭该轮，其后的第一条模型 chunk 属于新一轮——**不依赖 LangGraph 的 metadata**（那些是上游
内部键：`langgraph_step` 是会话级计数器、跨运行不重置，拿它划轮次在回放时会错位）。
它也是界面折叠的单位：**发起过工具调用的轮次是「过程」，没有工具调用的最后一轮就是最终回复**
（所以最终回复是**推导**出来的，不单独存）。
_Avoid_: 回合, step, turn, iteration

**RunJournal**：
一次运行的 RunEvent 序列，EvaAI 的可观测载体。它记录的是 **agent 产出的同一条事件流**（与 SSE 并列的两个消费者），不是另起一套埋点；可插拔的 store 决定它落在哪里（内存 / JSONL）。
_Avoid_: trace, 日志, telemetry

**mode（路径）**：
一轮运行走的是哪条路径，由 `run_start` 事件的 `mode` 字段承载：`react` 是主 Agent 直答，
`plan` 是已下线的 Plan-Exec。它决定气泡署名与面板文案，**不能靠「有没有计划事件」反推**
——那种推断在回放中途会时对时错。

T16 起只有一条路径（新运行写死的都是 `react`），这个字段**仍然要写**：前端 `runState.ts`
的缺省值是 `plan` 且那是**历史回退**语义（T8 之前的 journal 行里没有这个字段），不写的话
每轮新对话都会被标成 plan。所以它现在是一条**回放兼容**契约，不是路径分叉。
_Avoid_: 用 `plan` 事件是否存在来判断路径

**RunEvent**：
RunJournal 中的单条事件记录，也是 SSE 帧的来源。它住在 **runtime** 里——它是「一次运行说了
什么」的契约，与哪条路径在跑无关。
_Avoid_: log entry, span

### 参照

**DeerFlow**：
本项目的参照项目（`D:\pythonPro\deer-flow`），EvaAI 的层级与框架对齐它，但为「个人助手、本地部署」大幅裁剪。提到「参照 DeerFlow」时指对齐其思想，而非逐行照搬。
_Avoid_: 上游, 原项目
