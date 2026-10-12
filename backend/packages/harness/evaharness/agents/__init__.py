"""Agent 层：把图装起来的那一层（主 Agent 与子 agent）与治理中间件。

**它依赖 :mod:`evaharness.runtime`，不反过来**——事件契约、运行上下文、运行入口都在那里
（T19 搬过去，见 ADR-0024）。这条方向由 ``tests/test_harness_boundary.py`` 的一条守卫
测试钉死：``sandbox`` / ``knowledge`` / ``journal`` / ``models`` 这些机制包**不许**
import 本包。
"""
