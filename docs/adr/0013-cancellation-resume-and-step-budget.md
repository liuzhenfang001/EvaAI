# 主动取消、resume 收口与步数上限（T5 随 T8/T9 重新对齐）

issue #5 的文案写于 T8（ReAct 主 Agent 取代 Plan-Exec）与 T9（每请求建图）之前，其中
「取消整棵 subagent 树」「其余并行任务跑完 + 失败清单」对应的是**已封存的 Plan-Exec 路径**。
本片先把范围重新对齐到当前默认路径（单 lead agent + 一次性串行子 agent），再落地三条决策：
**硬取消做全链路、预算只做步数上限、resume 收口现状**。

## 决策

**1. 主动取消 = 取消内层 pump task，外层响应 task 仍存活。**

`run_lead_agent` 不再直接 `async for` 过 `agent.astream(...)`——那样没有一个能被 `cancel()`
触达的任务边界。改成「内层 langgraph 流跑在一个显式 `asyncio.Task`（pump）里，外层生成器经
队列消费」，这是 ADR-0006 里 fanout.py 的先例（`create_task` + sentinel，**不用** `TaskGroup`
——消费方提前放弃时 `TaskGroup` 抛 `BaseExceptionGroup`）。

- **主动取消**：`POST /chat/cancel` 经 `RunRegistry`（`run_id → pump task`）`pump.cancel()`；
  pump 放进 `("cancelled", None)` sentinel 后 re-raise；外层读到 sentinel、`gather(return_exceptions=True)`
  确认其已 cancelled → `yield RunEndEvent(status="cancelled")`。**外层响应 task 仍存活**，所以这个
  终态能经 SSE 送给还连着的前端、journal 也照常落盘（刷新后回放可见），再正常 `end`。
- **被动断连**：Starlette 取消外层响应 task → `CancelledError`（继承 `BaseException`，
  `except Exception` 接不住）在 `queue.get()` 处穿透；外层 `finally` 取消还没结束的 pump 并
  `unregister`，**不产** `cancelled` 事件。断开不伪装成「失败」或「取消」的运行（ADR-0005）。

harness 与 app 的边界靠 `RunCancellation` 这个小协议（`register(task)` / `unregister()`，
缺省 no-op）守住：harness 不知道 registry 长什么样，也不 import app。前端 `cancel()` 只发请求、
**不 abort fetch**——终态走同一条 SSE 流回来，`send()` 的 `for await` 自然走到 `finally` 把
`streaming` 置 false（与 ADR-0006「一个产出两个消费」一致，不搞前端乐观写入的第二条路径）。

**2. 预算只做步数上限，不做 token 累计。**

`StepBudgetMiddleware` 计数「带工具调用的模型响应」次数，按 checkpoint 坐标分桶
（`thread_bucket`），`before_agent` 重置（坐标跨轮复用，「步数上限」是一次运行内的概念）。超过
`max_steps` 时剥掉 `tool_calls` + 追加「已达步数上限，给出最终答复」文本，强制转文本收尾——
与 `LoopDetectionMiddleware` 的 hard-stop 同一个手法（共用抽出来的
`forced_stop.strip_tool_calls`），**不新增**「stopped」终态，run 仍以 `completed` 结束。

步数预算只接在主 Agent 链上（`build_lead_middlewares`），不进 `build_default_middlewares`：
子 agent 图仍由图级 recursion_limit + loop 检测兜住。默认 `max_steps=100`，远低于图级
`recursion_limit`——`create_agent` 编译时把它设为 9_999（不是 langgraph 的裸默认 25），
所以步数上限的优雅收尾总是先于那句看不懂的 `GraphRecursionError`。

不做 token 累计，是因为「步数」已经能兜住「无限换着花样跑下去」这个真实失败模式，而 token
累计要依赖 provider 报的准确用量（近似值会让阈值不可预测），收益对不上成本。将来真要做，是
`TokenUsageMiddleware` 的延伸，不是本片的事。

**3. resume 收口现状：同会话自动装载历史，不做断点续跑。**

「断点续跑」（从被中断的那一步接着跑）不做：它需要把「半截的工具调用」当作可恢复状态去续，
而当前的图模型里「再发一条消息」自然就是新的一轮、装载全部历史——被中断那轮的用户请求已经在
checkpoint 里，下一轮模型看得见、补得上。配合 `cancelled` 终态，把「发出去没得到回复」的半截
运行补成有明确终态的记录。

这条不是「没实现」，而是**明确选择了不收口的那一边**：断点续跑的复杂度（中断语义、部分结果的
归属、与 loop/预算中间件的交互）对本地个人助手的收益不成比例。

## 后果

- **事件契约**：`RunEndEvent.status` 从 `completed|failed` 扩到 `completed|failed|cancelled`。
  `cancelled` 是独立终态，**不是**失败——前端 note 写「已取消」而不是「运行失败」。
- **新增长命物**：`app.state.run_registry`（一个 dict，无活跃事件循环约束，与 checkpointer 并列）。
- **loop_detection 与 step_budget 共享** `forced_stop.strip_tool_calls`——「别再调工具，给最终答复」
  这一件事只有一个实现。
- **harness 边界测试**（`test_harness_boundary.py`）继续成立：`RunCancellation` 不 import app。

## 不做 / 范围外

- **取消整棵 subagent 树 / 并行 best-effort + 失败清单**：随 T8 封存废弃（见 ADR-0005 更新注）。
  默认路径是单 lead agent + 一次性串行子 agent，无树可取消；Plan-Exec 路径（`/chat/plan/stream`）
  仍不支持主动取消。
- **断点续跑**：见决策 3。
- **token 累计预算**：见决策 2。
- **跨 worker 取消**：`RunRegistry` 是单进程内的 dict，本地单 worker 够用；跨进程取消不是本片范围。
