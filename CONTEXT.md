# EvaAI

企业级可用标准的个人 AI 助手。本文件是 EvaAI 的**词汇表**：只定义本项目的语言，不含实现细节与设计决策（那些在 `docs/adr/`）。

## 语言

### 架构分层

**harness（框架层）**：
EvaAI 中业务无关、只提供可插拔机制的那一层。
_Avoid_: framework, 核心库, core, 基础设施

**app（业务层）**：
EvaAI 中装填具体实例（人设、工具、技能）的那一层。
_Avoid_: application, 业务代码, server

### 执行模型

**ReAct**：
一个「推理 → 行动 → 观察」的循环：模型自己决定调用哪个工具、调几次、何时收尾。
EvaAI 里所有 agent（主 Agent、子 agent、Plan-Exec 的任务执行单元）都是 ReAct 图。
_Avoid_: react agent, 循环体, agent loop

**lead agent（主 Agent）**：
项目**默认**的顶层 agent——一个 ReAct 图，用户直接与它对话。多轮记忆、会话摘要压缩、
工具调用都挂在它身上；它就是用户眼里那个「EvaAI」。
_Avoid_: orchestrator, supervisor, 总控, main agent, leader agent

**Plan-Exec**：
把用户请求拆成任务图、派发给执行单元、再汇总结果的编排模型。**T8 起不再是默认路径**，
退到 `/chat/plan/stream` 封存（路由与测试都保留，没有 UI 入口），将来以「主 Agent 的一个
子能力」回来。
_Avoid_: plan-and-execute, plan-and-solve, 规划执行

**planner**：
Plan-Exec 里产出任务图的角色（封存路径）。
_Avoid_: 规划器, 调度器, 拆解器

**replanner**：
Plan-Exec 里每批任务结束后判断「继续还是结束」的角色（不重写计划，封存路径）。
_Avoid_: 重规划器, 动态规划器

**任务执行单元**：
Plan-Exec 里承载单个任务的 ReAct 图（封存路径）。
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

### 装配

**建图工厂（AgentFactory）**：
按**请求**构建图与角色的唯一入口——主图、子 agent 图、模型，以及封存路径的
planner / 执行单元 / replanner。它只持有跨请求**必须**共享的建图参数（配置、业务工具、
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
模型看到的目录名（`/workspace`、`/output`、`/input`、`/skills`）与它们对应的宿主目录之间的
映射。**宿主绝对路径永远不进模型上下文**（泄漏 + 不可移植）。这四个挂载点是将来换真沙箱的
**唯一缝**。
_Avoid_: 沙箱路径, root path, 别名目录

**工作台（workspace）**：
会话级的工作目录，相对路径的落点，草稿与中间产物放这里。与「交付」（`/output`）和「用户上传」
（`/input`）分开，是为了让原件与成品都不被中间过程污染。
_Avoid_: tmp, 临时目录, sandbox

**只读挂载（read-only mount）**：
`/input` 与 `/skills` 的权限。前者因为改坏原件不可逆，后者因为可被模型改写的技能等于把提示
注入面交给它。T14 起 `/skills` 有了生产者——**启动期**把仓库里的技能源目录镜像过来（
:func:`app.gateway.skills.project_skills`），所以它既不按会话分，也不由谁在会话里创建。
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

### 交付

**垂直切片（tracer bullet）**：
一条端到端打穿各层、自身即可演示或验证的交付单元。
_Avoid_: milestone, 里程碑, 阶段, horizontal slice

**T1–T14**：
当前垂直切片的编号。**T 号与 GitHub Issue 号不一一对应**（T3 是 #4、T4 是 #3、T7 是 #7、T13 是 #14、T14 是 #15），引用时写全「T3（#4）」以免混淆。T7 起都是原 MVP（T1–T6）之外新增的：T7 见 [Issue #7](https://github.com/liuzhenfang001/EvaAI/issues/7)，T8（换成 ReAct 主 Agent）见 [Issue #8](https://github.com/liuzhenfang001/EvaAI/issues/8)，T9（图改为每请求构建）见 [Issue #9](https://github.com/liuzhenfang001/EvaAI/issues/9)，T10（回复渲染 Markdown）见 [Issue #10](https://github.com/liuzhenfang001/EvaAI/issues/10)，T11（产出物以文件卡片展示）见 [Issue #11](https://github.com/liuzhenfang001/EvaAI/issues/11)，T12（思考模式与动态深度）见 [Issue #12](https://github.com/liuzhenfang001/EvaAI/issues/12)，T13（MCP 集成）见 [Issue #14](https://github.com/liuzhenfang001/EvaAI/issues/14)，T14（skill 加载机制）见 [Issue #15](https://github.com/liuzhenfang001/EvaAI/issues/15)。二期到 T12 收尾，T13 起是[三期](https://github.com/liuzhenfang001/EvaAI/issues/13)。
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
| Plan-Exec 任务 | `{会话}:{运行}:{任务}` | 运行级（封存路径） |

它也是 LoopDetection 的分桶键，并出现在 RunEvent 的 `metadata.thread_id` 里
（那是 LangGraph 自己的键，我们原样保留）。**账本本身活在每请求一份的中间件实例里**，
随请求消失——所以坐标跨轮复用（`{会话}:lead` 就是）不会让账本跨轮累积。
_Avoid_: 与「thread（会话）」混用；读 `metadata.thread_id` 时误以为它是会话 id

**lane（泳道）**：
RunEvent 的 `task_id` 在界面上的分组。主 Agent 用约定的 `main`，Plan-Exec 用 `t1`/`t2`。
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
一轮运行走的是哪条路径，由 `run_start` 事件的 `mode` 字段承载：`react` 是主 Agent 直答
（默认），`plan` 是封存的 Plan-Exec。它决定气泡署名与面板文案，**不能靠「有没有计划事件」
反推**——那种推断在回放中途会时对时错。
_Avoid_: 用 `plan` 事件是否存在来判断路径

**RunEvent**：
RunJournal 中的单条事件记录。
_Avoid_: log entry, span

### 参照

**DeerFlow**：
本项目的参照项目（`D:\pythonPro\deer-flow`），EvaAI 的层级与框架对齐它，但为「个人助手、本地部署」大幅裁剪。提到「参照 DeerFlow」时指对齐其思想，而非逐行照搬。
_Avoid_: 上游, 原项目
