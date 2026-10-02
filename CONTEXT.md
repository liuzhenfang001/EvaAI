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

**subagent（子 agent）**：
由主 Agent 通过 `task` 工具派发出去、独立完成**一件事**的 ReAct 图。它看不到父对话，
中间步骤也不会回流到主对话——**上下文隔离才是它存在的理由**，不是「更快」。
_Avoid_: worker, 任务 agent, child agent

### 交付

**垂直切片（tracer bullet）**：
一条端到端打穿各层、自身即可演示或验证的交付单元。
_Avoid_: milestone, 里程碑, 阶段, horizontal slice

**T1–T8**：
当前垂直切片的编号。**T 号与 GitHub Issue 号不一一对应**（T3 是 #4、T4 是 #3、T7 是 #7），引用时写全「T3（#4）」以免混淆。T7 与 T8 都是原 MVP（T1–T6）之外新增的：T7 见 [Issue #7](https://github.com/liuzhenfang001/EvaAI/issues/7)，T8（换成 ReAct 主 Agent）见 [Issue #8](https://github.com/liuzhenfang001/EvaAI/issues/8)。
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
（那是 LangGraph 自己的键，我们原样保留）。
_Avoid_: 与「thread（会话）」混用；读 `metadata.thread_id` 时误以为它是会话 id

**lane（泳道）**：
RunEvent 的 `task_id` 在界面上的分组。主 Agent 用约定的 `main`，Plan-Exec 用 `t1`/`t2`。
它**跨运行会重复**（同一个会话的第二轮又会出现 `t1`），所以只能当展示标签、不能当身份；
气泡身份是 `runId:lane`。
_Avoid_: 用 lane 当消息 id；把它当成任务的持久标识

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
