# 编排载体用 async 生成器，不搭 LangGraph 图

ADR-0004 定了「顶层用 Plan-And-Executor 显式编排」，但没说「显式」要用什么承载。本 ADR 把它落到**纯 async 生成器 + RunEvent 流**：`run_plan_exec()` 是个 async 生成器，planner / ReAct executor / replanner 都是它的注入参数，编排进度活在 Python 控制流里。ReAct 执行器内部仍是 `create_agent` 编译出的 LangGraph（ADR-0003 不变），两者是嵌套而非同构。

三个理由：**可测**——三个角色都是注入点，契约测试全用替身驱动，不必围着 compiled graph 转；**可取消**——ADR-0005 的硬取消就是 `asyncio.Task.cancel()` 穿透整棵调用树，不需要理解 checkpointer 的中断语义；**可观测**——事件流本身就是 SSE 与 T3 运行时间线的共同来源，一个产出两个消费，不用为「流式」和「事件」维护两套通道。

## 考虑过的选项

- **LangGraph 三节点图（planner / executor / replanner）**：更贴 ADR-0004 里「图」的字面，且同进程恢复可以整包交给 checkpointer。代价是图套图——外层图的状态与内层 `create_agent` 图的状态各自序列化，流式要混用 `messages` / `updates` / `custom` 三种模式，契约测试也得先编译图再断言。为「措辞贴合」付这些复杂度不划算。
- **单一 lead agent + todo 中间件**（参照项目 DeerFlow 当前的做法）：最省事，但退回了「任务级规划不可见」——计划只活在模型上下文里，前端拿不到结构化的任务与依赖。T2 的验收要求计划可观察，这条路直接不满足。

## 后果

- **编排进度不在任何 checkpoint 里**。`SqliteSaver` 保存的是每个 ReAct 任务图内部的 state（ADR-0007），而「plan 是什么、哪些 task 已完成、剩余队列」只在 Python 栈帧里。所以 T5 要跨重启恢复**编排**，仍需另加一个 run registry（按 `run_id` 存 RunJournal + 任务状态），或接受「恢复 = 重跑整条编排」。这是 T5 的先决条件，不是它能顺手解决的。
- **T3 的并行批次是 `asyncio.gather`**，不是 LangGraph 的 superstep。好处是并发控制（信号量、分批）直接可写；代价是没有现成的超步级检查点。
- 编排器的输出契约是 `RunEvent` 判别联合，加新事件类型是加法。但**线上有两套 data 形状**（`messages` 帧是 `[chunk, metadata]` 数组以兼容 T1，其余是对象），T3 若要统一会是一次破坏性变更。
