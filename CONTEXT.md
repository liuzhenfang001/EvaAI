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

**Plan-And-Executor（Plan-Exec）**：
把用户请求拆成任务图、派发给执行单元、再汇总结果的顶层编排模型。
_Avoid_: plan-and-execute, plan-and-solve, 规划执行

**ReAct 执行器**：
承载单个任务的执行单元，在一个推理-行动循环里调用工具直到该任务完成。
_Avoid_: react agent, worker, 循环体

**lead agent（主 agent）**：
承载 Plan-Exec 编排图的顶层 agent。
_Avoid_: orchestrator, supervisor, 总控, main agent

**subagent（子 agent）**：
由 lead agent 派发、独立执行单个任务的执行单元。
_Avoid_: worker, 任务 agent, child agent

**planner**：
产出任务图的角色。
_Avoid_: 规划器, 调度器, 拆解器

**replanner**：
每批任务结束后判断「继续还是结束」的角色（不重写计划）。
_Avoid_: 重规划器, 动态规划器

### 交付

**垂直切片（tracer bullet）**：
一条端到端打穿各层、自身即可演示或验证的交付单元。
_Avoid_: milestone, 里程碑, 阶段, horizontal slice

**T1–T7**：
当前垂直切片的编号。**T 号与 GitHub Issue 号不一一对应**（T3 是 #4、T4 是 #3、T7 是 #7），引用时写全「T3（#4）」以免混淆。T7 是原 MVP（T1–T6）之外新增的一条，见 [Issue #7](https://github.com/liuzhenfang001/EvaAI/issues/7)。
_Avoid_: M2/M3/M4（旧的水平里程碑分法，已废弃）

### 可观测

**thread（会话）**：
一段连续的对话，由多次运行组成。它不是一张表里的实体，而是 RunJournal 按 `thread_id` 的分组——会话由第一次运行落盘时自然形成。
_Avoid_: session, conversation, 聊天记录

**checkpoint thread**：
**任务级** checkpoint 的坐标，形如 `{会话}:{运行}:{任务}`。与「会话」同名但不同层：它标的是「哪个会话的哪次运行的哪个任务的那张 ReAct 图」，用于隔离图状态、给 LoopDetection 分桶、以及让「删会话」能按前缀连带清理。它出现在 RunEvent 的 `metadata.thread_id` 里（那是 LangGraph 自己的键，我们原样保留）。
_Avoid_: 与「thread（会话）」混用；读 `metadata.thread_id` 时误以为它是会话 id

**RunJournal**：
一次运行的 RunEvent 序列，EvaAI 的可观测载体。它记录的是**编排器的同一条事件流**（与 SSE 并列的两个消费者），不是另起一套埋点；可插拔的 store 决定它落在哪里（内存 / JSONL）。
_Avoid_: trace, 日志, telemetry

**RunEvent**：
RunJournal 中的单条事件记录。
_Avoid_: log entry, span

### 参照

**DeerFlow**：
本项目的参照项目（`D:\pythonPro\deer-flow`），EvaAI 的层级与框架对齐它，但为「个人助手、本地部署」大幅裁剪。提到「参照 DeerFlow」时指对齐其思想，而非逐行照搬。
_Avoid_: 上游, 原项目
