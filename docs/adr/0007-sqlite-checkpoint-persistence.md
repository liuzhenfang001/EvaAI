# checkpoint 持久化改用 SQLite（取代 ADR-0005 的「恢复限定同进程」）

T2 起，每个 ReAct 任务图的 state 落到 SQLite（`langgraph-checkpoint-sqlite` 的 `AsyncSqliteSaver`），thread 按 `run_id:task_id` 划分。这**部分取代 ADR-0005**：该条说的「恢复限定同进程、用 `MemorySaver`、跨重启持久化列入二期」不再成立，硬取消与失败 best-effort 两条仍然有效。

## 澄清：这买不到「重启后接着跑」

持久化的是**任务级 ReAct state**，不是编排进度——后者活在 Python 栈帧里（ADR-0006）。重启后端之后，`checkpoints` 表里确实躺着上一个 run 的任务状态，但没有任何东西知道该从哪里续跑：plan 是什么、哪些任务已完成、剩余队列是什么，全都不在库里。真正要跨重启恢复编排，还需要一个 run registry；本 ADR 只是把「任务状态」这一半先落地，让 T5 面对的问题少一半。

## 配套的保留策略

`checkpoints` 表没有时间戳列，所以「按天清理」是从 `checkpoint_id` 反解时间：它是 UUIDv6，时间有序，但 version nibble 插在时间字段中间，必须按「48 位高位 + 12 位低位」拼接而非取一段掩码。启动时按 `checkpoint.retention_days`（默认 7 天）清理过期 thread；`<= 0` 表示不清理。反解不出时间的 thread 一律保留——宁可不删，不可误删。

## 考虑过的选项

- **继续用 `MemorySaver`，把持久化推到二期**（ADR-0005 的原计划）：T2 本身确实只需要进程内状态，`stream_mode="values"` 取终态已够用。代价是等 T3 并行、T5 中断落地之后再补，持久化的数据形状要重新设计一轮，而那时已经有更多代码压在它上面。
- **同时上 run registry，把 T5 的恢复做实**：能让「重启继续」真的可用。代价是 T2 撑大一圈，且 ADR-0005 的二期边界被整体推翻——当时的判断是这属于 T5 的范围，现在仍然成立。

## 后果

- harness 多一个依赖（`langgraph-checkpoint-sqlite`，带出 `aiosqlite`）。`AsyncSqliteSaver` 的构造依赖运行中的事件循环，**只能在 lifespan 协程里建**，不能在模块导入期建；长生命周期由 `AsyncExitStack` 持有。
- `thread_id` 必须走 `config={"configurable": {"thread_id": ...}}`——只传 `context` 会直接 `ValueError`。而 `LoopDetectionMiddleware` 读的是 `runtime.context`，所以两个都得传，且值相同（见 `executor.py`）。
- 每个 run 都会留下 N 个 thread（N = 任务数），DB 靠启动时清理保持有界，而不是 run 结束即删——保留正是加 SQLite 的目的。
- `data/` 加入 `.gitignore`。
