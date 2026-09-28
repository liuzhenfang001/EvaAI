# Plan-Exec 与 ReAct 是嵌套关系，不是二选一

顶层用 Plan-And-Executor 显式编排图（planner → 派发 ReAct 执行单元 → 汇总），其中每个任务由一个 ReAct 执行器执行；两者嵌套而非互斥。参考项目 DeerFlow 是纯 ReAct，在长任务上容易失焦；而纯 Plan-Exec 又缺少单个任务内部的工具迭代能力——嵌套同时拿到任务级规划与任务内推理-行动循环。
