# 停止 = 硬取消；失败 = best-effort；恢复限定同进程

> **2026-09-28 更新**：本决策的第三句（「中断恢复限定在同进程内」）已由 [ADR-0007](0007-sqlite-checkpoint-persistence.md) 取代——T2 起 checkpoint 落 SQLite。硬取消与失败 best-effort 两条仍然有效。

用户停止采用立即硬取消（asyncio task cancel 整棵 subagent 树），不做软停止的优雅收尾。任务失败采用 best-effort：失败任务标记失败，其余并行任务照常跑完，汇总时返回部分结果加失败清单。中断恢复限定在同进程内（LangGraph `MemorySaver`），不跨重启。

## 考虑过的选项

- **失败即 fail-fast，取消全部并行任务**：语义干净，但个人助手场景里「部分结果 + 哪几个没跑成」通常比「什么都没有」有用得多。
- **跨重启持久化 checkpoint**：更强的恢复能力，但需要 sqlite/外部存储，列入二期而非 MVP。
