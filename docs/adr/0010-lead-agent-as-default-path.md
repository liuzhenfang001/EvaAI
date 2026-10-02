# ReAct 主 Agent 取代 Plan-Exec 成为默认路径

> **2026-10-02 更新**：决策 1 里「`app.state` 里仍然装配」这一句已由 [ADR-0011](0011-per-request-graph-construction.md) 取代——T9 起 Plan-Exec 的图也改为**每请求构建**，`app.state` 上只剩长命物。封存本身（路由保留、测试保留、没有 UI 入口）不变。另：下面「后果」里「`LoopDetectionMiddleware` 的 `_history` 对 Plan-Exec 的坐标仍是无界增长」一条，也随「中间件实例随请求消失」而解除。

T1–T7 一路把 **Plan-Exec 当作主路径**：任何一句「你好」都要先过 planner，再由执行单元
执行，replanner 复核。这个前提是错的——默认应当是**直答**，需要时才升级为规划执行。
本 ADR 记录这次降级、新的默认架构，以及实现中实测出来的三个坑。

## 决策

**1. 默认路径是一个 ReAct 主 Agent（lead agent），Plan-Exec 退到 `/chat/plan/stream` 封存。**

「封存」的确切含义：路由保留、测试保留、`app.state` 里仍然装配，**没有 UI 入口**。
不删的理由是它并非一无是处——计划面板让用户**看得见它打算做什么**、它真的**并行**、
它的 planner 是纯函数好测好换。将来的回归形态**本片不定**，接缝留空。

**2. 主 Agent 直接用 `create_agent` 装配，不复用 `make_react_agent`。**

不共用封装是因为两者的差异是**真实的**，不是参数不同：主 Agent 有 `context_schema`
（子 agent 派发靠它取运行身份）、有会话级 checkpoint 坐标（记忆的来源）、中间件链里多
一道摘要压缩；而 Plan-Exec 的执行单元是「一次任务、一个独立坐标、没有记忆」。
共用一个薄封装只会让两边互相牵制。`make_react_agent` 原样留给封存路径。

**3. 多轮记忆靠**会话级固定坐标**让 checkpointer 自动装载，不显式组装 history。**

坐标是 `{会话}:lead`（`lead_thread_id`），**跨轮不变**——这是记忆的全部来源。实测：
同一个 `thread_id` 第二轮模型看到 6 条消息（首轮 4 条），换一个 `thread_id` 回到 2 条。

这是本片对「历史要分三处组装」那个问题的回答：**不分头组装，而是让只有一张图、一个
坐标**，历史由 LangGraph 自己带进来。摘要压缩也因此在结构上变得可做——压缩的对象
「一段会话的消息列表」现在真实存在了。

坐标仍以 `{会话}:` 开头，所以 `delete_session_checkpoints` 的前缀扫描天然覆盖它，
删会话连记忆一起删。

**4. 新增 `RunEndEvent`；`RunStartEvent` 加 `mode`。**

主 Agent 不拆任务，所以 message 桶里到它为止**再也没有一个终态事件**，而「助手这一轮
说完了」正是对话要显示的东西。它同时是 journal 的提交点——`RunJournal` 按条目数计的
阈值在归并之后很少触发，收尾强制 flush 才能把崩溃窗口限制在单次运行内（原先只有
`task_end` 有这个待遇）。

`mode`（`react` / `plan`）缺省 `"plan"` 是**历史回退**而不是语义缺省：T8 之前写下的
journal 行里没有这个字段，而那些行确实都是 Plan-Exec 写的。前端靠它决定署名、面板
文案与「正在思考」占位——**不能靠「有没有 plan 事件」反推**，那种推断在回放中途会
时对时错。

**5. 子 agent 由 `task` 工具派发，坐标是运行级 + 调用级。**

`{会话}:{运行}:sub:{tool_call_id}`。用 `tool_call_id` 而不是自增计数器：天然唯一、
可重入、不需要任何跨运行状态。运行级坐标 → 子 agent **跨轮不累积**，与主 Agent 的
会话级固定坐标正相反（它是短工，不是对话）。

防递归是**结构性**的：子 agent 是独立编译的图，里面根本没有 `task` 工具；
`make_subagent_graph` 收到同名工具会直接抛。

子 agent 用 `ainvoke` 跑到底，只把结论作为工具返回值交回——本片**不把它的中间过程
流出去**。`runtime.stream_writer` 字段已确认存在，是将来把它流成独立泳道的现成接缝。

**6. 摘要压缩挂 langchain 内置的 `SummarizationMiddleware`，位置在默认链**之后**。**

排在最后（最内层）是有理由的：它在 `before_model` 里改 state，排在最后意味着它是最后
一道改消息的工序——量到的 token 数就是模型真正会收到的数量（`InputSanitizationMiddleware`
的包裹与 `LoopDetectionMiddleware` 的警告注入都已经生效）。

## 实现中实测出来的三个坑

**① `LoopDetectionMiddleware` 的分桶永不重置——会话级坐标会把它变成误判源。**

它的追踪状态按 `runtime.context["thread_id"]` 分桶且进程内不重置。Plan-Exec 里每个任务
一个坐标、桶从不复用，所以问题被掩盖了；而主 Agent 的坐标是**会话级固定**的——同一个
工具在第 3 轮就会撞上 `warn_threshold=3`，第 5 轮直接 `hard_limit` 被剥掉 `tool_calls`
**强行掐断**。症状是「用户的正常提问突然答不出来」，极难反推到循环检测。

修法是加 `before_agent` 调 `self.reset(thread_id)`。语义上也是对的：**循环是一次运行内
的现象，不是跨轮的**。`TerminalResponseMiddleware` 已经在 `before_agent` 里重置自己的
重试预算，这是同一个先例。

**② 在工具里调另一张图，会继承宿主运行的整套 pregel 内部键。**

`task` 工具调 `subagent.ainvoke(...)` 时，langgraph 会把宿主的 `configurable` **合并**
进来（不是替换）：`__pregel_read`、`__pregel_scratchpad`、`checkpoint_ns`、
`__pregel_checkpointer`……于是子 agent 的输入进了**父图的 channel** 而不是自己的，
工具调用不会被路由。

这个坑最恶劣的地方是它**完全静默**：子 agent 不报错，只是「跑一步就停」，返回值退化成
模型产出的第一段文本。实测模型调用次数从 2 掉到 1、工具一次都没跑，链路上没有任何异常；
而当时已有的那几条测试**照样全绿**（断言只看了 prompt 内容，没看它有没有真的干活）。

修法是在调用前把 `var_child_runnable_config` 置空、`finally` 还原
（`subagent._run_isolated`）。语义上这也对：子 agent 是**独立的一次运行**（自己的
checkpointer、自己的 thread、自己的预算），不是父图 pregel 循环的子节点。代价是它的
run 不再挂在父运行的 callback 树上——我们不做 tracing，不依赖这一点。

**③ 两个中间件的分桶键只认 `dict` 上下文——带类型的上下文会全部落进兜底桶。**

`LoopDetectionMiddleware` 与 `TerminalResponseMiddleware` 各自有一份**一模一样**的
`_thread_id`，都只认 `runtime.context` 是 dict 的情况。Plan-Exec 的任务执行单元传的正好是
`{"thread_id": ...}`，所以一直没暴露；T8 换成带类型的 `LeadContext` 之后，两者都掉进
`"default"`——**所有会话、所有子 agent 共用一本账**。

后果有两层，第二层更隐蔽：并发会话互相累加、谁先撞上限谁先被掐断；而且**子 agent 的
`before_agent` 会把主 Agent 正在累积的循环检测历史清空**，等于主 Agent 派一次子 agent
就自缴一次械。症状全是「偶尔」「莫名」，从行为反推不到上下文形状上。

修法是抽成共用的 `middlewares/context.py:thread_bucket`，两种形状都认。**抽出来本身就
是修复的一部分**：两份重复实现意味着改一处忘一处，从而得到「一个中间件按坐标分桶、
另一个按 default 分桶」这种最难查的不一致。

**④ 测试替身的流式路径不携带 `tool_calls`。**

`GenericFakeChatModel._stream` 只搬 `content`（它只特判了遗留的
`additional_kwargs["function_call"]`），`tool_calls` 会被丢掉，所以 `ToolCapableFake`
能驱动纯文本却**驱动不了工具往返**；而 `content=""` 的工具调用消息在流式下一个 chunk
都产不出，直接 `ValueError: No generations found in stream`。

同样是静默的：测试不会失败，只会测到一条没有工具调用的路径。新增的 `StreamToolFake`
一次调用产出一个整块 chunk 并带上 `tool_calls`，专供这类测试。

## 后果

- **主 Agent 会「失忆」而界面不会**：`prune_checkpoints`（默认 7 天）删掉的是 checkpoint，
  journal 还在——对话看得见、模型想不起来。本片**接受**这个语义（记忆是缓存，journal
  才是记录）。将来若要把记忆重建回来是可行的（journal 里有完整消息），另开切片。
- **摘要压缩改的是 checkpoint 的 state，journal 看不到**，于是「模型记得的」与「界面上
  显示的」会分叉：界面永远是全文，模型看到的是摘要。这是设计使然（journal = 记录，
  checkpoint = 记忆），但必须写在这里，否则后会被当成 bug。
- **子 agent 的中间过程在界面上不可见**：只看到 `调用 task` 与 `task 返回结果`。这是
  本片有意划的界，接缝已经留好。
- **`LoopDetectionMiddleware` 的 `_history` 对 Plan-Exec 的坐标仍是无界增长**（每个任务
  一条，永不回收）。加了 `before_agent` 重置后主 Agent 这条线是干净的；封存路径那条是
  既有问题，本片只记不修。
- **人设终于有了主人**。T2–T7 期间 `app/prompts.py` 的 `SYSTEM_PROMPT` 是死代码，用户
  的问题落在内部角色身上——实测出现过用户问「你可以做什么」而被回答「我是 **EvaAI 的
  规划器**」。现在顶层是一个真正的助手，那份提示词重新有了消费者。
- **`mode` 的缺省值 `"plan"` 是历史回退，不是语义缺省**。新事件类型若也想要缺省值，
  不要照抄这个先例。
- **遗留债：`events.py` 仍在 `agents/plan_exec/` 下**，而它现在是两条路径共用的契约。
  搬到 `agents/events.py` 才是正确位置，但那会牵动十几个 import 与 `schemas` 里几个
  事件载荷类型的归属（`PlanTask` / `TaskResult` / `TerminalTaskStatus`），本片按「不扩大
  范围」留下。**Plan-Exec 的最终去向定了之后应该一并处理。**
- **`config.yaml` 的 `log_level` 仍无人消费**，所以摘要压缩、prune 结果、token 用量这些
  `logger.info` 全都不可见。
- **前端仍无测试基建**。本片又一次靠在 node 里单编纯 reducer 来验（`applyFrame` 的
  `run_end` 分支、`mode` 回退、气泡署名），并做了非空洞性检查。这已经是第三次用手工
  方式发现真实 bug，值得尽快补 vitest。
