# 停止 = 硬取消；失败 = best-effort；恢复限定同进程

> **2026-09-28 更新**：本决策的第三句（「中断恢复限定在同进程内」）已由 [ADR-0007](0007-sqlite-checkpoint-persistence.md) 取代——T2 起 checkpoint 落 SQLite。硬取消与失败 best-effort 两条仍然有效。
>
> **2026-10-07 更新**：第一句里的两条具体机制随 T8 封存废弃——默认路径已从 Plan-Exec 换成**单 lead agent + 一次性串行子 agent**（[ADR-0010](0010-lead-agent-as-default-path.md)），「取消整棵 subagent 树」无树可取消，「其余并行任务跑完 + 失败清单」无并行可汇总。硬取消与失败 best-effort 的**原则**不变，只是落点由 [ADR-0013](0013-cancellation-resume-and-step-budget.md) 收口为：主动取消 = 取消内层 pump task（产 `run_end(status="cancelled")`），被动断连仍是不产终态的硬穿透。

用户停止采用立即硬取消（asyncio task cancel 整棵 subagent 树），不做软停止的优雅收尾。任务失败采用 best-effort：失败任务标记失败，其余并行任务照常跑完，汇总时返回部分结果加失败清单。中断恢复限定在同进程内（LangGraph `MemorySaver`），不跨重启。

## 考虑过的选项

- **失败即 fail-fast，取消全部并行任务**：语义干净，但个人助手场景里「部分结果 + 哪几个没跑成」通常比「什么都没有」有用得多。
- **跨重启持久化 checkpoint**：更强的恢复能力，但需要 sqlite/外部存储，列入二期而非 MVP。
