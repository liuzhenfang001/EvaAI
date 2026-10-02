"""从运行上下文里取「这一轮属于哪个桶」。

中间件要按运行分桶（循环检测的历史、兜底重试的预算），桶键就是**检查点坐标**。
而运行的上下文有两种形状，两种都得认：

- **dict**：Plan-Exec 的任务执行单元传 ``{"thread_id": ...}``（早期形状）；
- **对象**：lead agent 与子 agent 传 :class:`~evaharness.agents.lead_agent.LeadContext`
  ——它们要 ``session_id`` / ``run_id`` 供 ``task`` 工具派发，所以用带类型的上下文。

只认其中一种的后果是**静默的**：取不到就落进 ``"default"`` 兜底桶，于是所有会话、
所有子 agent 共用一本账——两个并发会话互相累加，而子 agent 的 ``before_agent``
还会顺手把主 Agent 正在累积的历史清掉。

放在这里而不是各中间件里各写一份：原先两处各有一份**一模一样的**实现，改一处忘一处
就成了「一个中间件按坐标分桶、另一个按 default 分桶」这种最难查的不一致。
"""

from __future__ import annotations

from collections.abc import Mapping

from langgraph.runtime import Runtime

FALLBACK_BUCKET = "default"
"""取不到坐标时的兜底桶。

它意味着「所有取不到坐标的运行共用一本账」，所以**只在真的没有上下文时**才该出现
（例如不带 context 直接跑图的单元测试）。正常路径上每个 run 都有自己的坐标。
"""


def thread_bucket(runtime: Runtime) -> str:
    """取这一轮的分桶键（检查点坐标），认 dict 与带 ``thread_id`` 属性的对象两种形状。"""
    context = getattr(runtime, "context", None)
    if context is None:
        return FALLBACK_BUCKET
    value = context.get("thread_id") if isinstance(context, Mapping) else getattr(context, "thread_id", None)
    return str(value) if value else FALLBACK_BUCKET


__all__ = ["FALLBACK_BUCKET", "thread_bucket"]
