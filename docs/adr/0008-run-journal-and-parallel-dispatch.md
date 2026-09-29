# 并行按批派发；RunJournal 是事件流的 sink，不是 callback handler

T3（Issue #4）要三件事：多个无依赖子任务并行执行、RunJournal 记录完整 RunEvent 供前端时间线回放、子任务结果合并后汇总返回（这条 T2 已具备）。本 ADR 记录这次落地的几个判断。

## 决策

**1. 派发按批并发，批由依赖决定。** 编排器每轮取**全部**就绪任务（而非第一个）一起派发，事件经 `merge_named_streams` 合流回单条 RunEvent 流——输出契约没变，变的只是产生方式。一批跑完再算下一批，所以没有依赖关系的任务真正同时跑，有依赖的仍严格串行。不做并发上限：个人助手的计划规模小，将来需要时在合流外面加信号量即可，不动既有契约。

**2. 合流用显式 `asyncio.create_task` + sentinel，不用 `asyncio.TaskGroup`。** 这是实测结论：消费方提前放弃时（SSE 客户端断连会走到 `aclose()`），TaskGroup 的 `__aexit__` 把子任务的取消包成 `BaseExceptionGroup` 抛出来，SSE 层会当成未处理异常。显式写法在四种退出路径下都干净——跑完 / 放弃（不抛）/ 取消（`CancelledError` 原样穿透，满足 ADR-0005 的硬取消）/ 子流崩溃（作为数据交回，同批其余照跑）。sentinel 放在子泵的 `finally` 里，所以「抛异常」与「正常耗尽」都会递减计数；收尾用 `await asyncio.gather(*pumps, return_exceptions=True)`，实测在取消路径下也能完成，所以不必做「不 await 的脏收尾」——那样「取消是否穿透到子流」就不可断言了。

**3. RunJournal 是事件流上的 sink（tee），不是 LangChain 的 `BaseCallbackHandler`。** 参照项目 DeerFlow 的 `RunJournal` 是 callback handler，因为它**没有统一的事件流**——事件靠 `config["callbacks"]` 注册、从 `on_chain_start` / `on_llm_end` 等回调里逐个捕获，并刻意不实现 `on_llm_new_token`。EvaAI 不需要这一层：ADR-0006 已经定了「事件流本身就是 SSE 与运行时间线的共同来源，一个产出两个消费」。所以 journal 只是在同一条流上再挂一个消费者（`journaled` tee）。副作用是写入天然串行，因此**不需要** DeerFlow 那套 per-thread 写锁——那是给多路并发 callback 准备的，在这里是为不存在的问题加锁。

**4. 并行归属靠 `metadata.task_id`，全序靠 `seq`，没有 span 树。** 与 DeerFlow 一致（它同样没有 `parent_id` / `span_id`，并行子 agent 靠 `task_id` 区分）。EvaAI 的 `seq` 由 `RunJournal` 在记录时分配（单消费者，不必劳烦 store）。为此 `MessagesEvent` 增加了必填的 `task_id`：并行后 token 交错到达，没有归属信息前端就无法分流到各自的气泡与泳道。

**5. 记账失败不拖垮运行。** journal 是可观测设施，不是关键路径。`journaled` 把 store 的异常吞成警告——磁盘满了不该让用户的请求失败；`RunJournal.flush` 自身仍会抛（并把缓冲放回），好让直接调用它的代码能察觉。

## 后果

- **replanner 的咨询时机变了：每批一次，且只在下游还有活可干时。** 这是「每批任务结束后判定」（CONTEXT.md）在并行下的正确推论，不是能力缩水：两个互不依赖的任务现在并成一批跑完，此时没有剩余就绪任务，问了也只能得到「没活了」——不问才对，这也正是 T2 刻意避免的「为必然的答案付一次 LLM 调用」。代价是 T2 里 5 条用互不依赖计划写的测试前提失效，已按新语义改写，并补了一条 `test_replanner_not_called_when_the_whole_plan_ran_in_one_batch` 把新行为显式钉住。
- **逐 token 记录是 journal 体积的增长源。** `MessagesEvent` 是每个 token 一条，一次运行成百上千条。为此加了 `flush_threshold`（攒够一批才落一次 store）——JSONL 的 append 是「一次批量 = 一次开/写/关文件」，阈值取 1 会让每个 token 都触发一次文件写。本地个人助手规模下可接受；T5 若把 journal 当作恢复源，需要截断或合并策略，那时才有必要做。
- **「回放」重建的是执行过程，不含用户原始请求。** journal 记的是 RunEvent，而用户请求不是 RunEvent——`plan` 是第一个事件。所以回放视图里有计划、时间线、各任务气泡，但没有「你问了什么」。`PlanEvent` 里已经有任务描述，够用；若日后确实需要，应当新增一个 run 元事件（如 `run.start`）来承载请求与时间等元信息，而不是继续往 `plan` 上堆字段。前端目前用一条「回放：xxx（历史记录，非实时）」的提示把这件事讲清楚。
- **前端的时间线没有参照实现。** DeerFlow 前端只有子任务卡内部按 `message_index` 排的步骤，没有全局 trace 时间线，也没有并行泳道——这部分是设计判断而非照搬。当前取的是**扁平日志**（序号 / 泳道 / 图标 / 文本），而不是甘特图式的泳道：并行与否看相邻行的泳道是否交替即可，实现和阅读成本都低得多。
- **回放的正确性依赖前端的纯函数投影，而前端没有测试基建。** 把「事件 → 状态」抽成纯函数 `applyFrame` 正是为了让实时与回放**共用同一套投影**（否则回放是一条独立实现，必然漂移），也让它将来的可测性成为可能。本轮仍靠 `tsc --noEmit` 与手工验收；补 vitest 另开切片。
- **`PlanEvent.run_id` 让「计划」事件承担了 run 级元信息。** 这个位置是权宜：前端要在第一个事件上就拿到回放凭据。同样地，更多 run 元信息应当升格为独立的 run 元事件。
