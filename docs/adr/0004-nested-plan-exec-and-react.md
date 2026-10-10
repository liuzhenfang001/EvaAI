# Plan-Exec 与 ReAct 是嵌套关系，不是二选一

> **2026-10-10 起作废**：Plan-Exec 已在 T16 整体下线（见 [ADR-0021](0021-plan-exec-retirement.md)）——T8 起它就不再是默认路径，T16 把封存的那套也删了。本 ADR 保留作历史记录：它解释了当年为什么是嵌套，以及后来为什么不再需要。

顶层用 Plan-And-Executor 显式编排图（planner → 派发 ReAct 执行单元 → 汇总），其中每个任务由一个 ReAct 执行器执行；两者嵌套而非互斥。参考项目 DeerFlow 是纯 ReAct，在长任务上容易失焦；而纯 Plan-Exec 又缺少单个任务内部的工具迭代能力——嵌套同时拿到任务级规划与任务内推理-行动循环。
