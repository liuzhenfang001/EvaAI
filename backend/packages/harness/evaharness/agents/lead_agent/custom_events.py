"""工具通过 langgraph 的 custom 流把消息送进事件流时用的载荷契约。

**为什么工具不直接构造 RunEvent**：工具不知道自己在哪条泳道上（那是 runner 的职责），而且
``events`` 这个模块 import ``LEAD_LANE`` 会与 :mod:`~evaharness.agents.lead_agent.agent`
循环依赖。所以分工是——工具 emit 一个**载荷**，runner 认它、补上泳道、构造并校验成
``RunEvent``。``RunEvent`` 仍是唯一输出契约，工具只是多了一个生产者（ADR-0006 的
「一个产出两个消费」不变）。

**为什么单独一个模块**：它是 harness 与 app 的**共用边界**——生产者在 app（业务工具要
``Sandbox`` 才能分辨交付物），消费者在 harness（runner）。放 ``events.py`` 里会把上面那处
循环依赖带进来；放 runner 里，app 就得 import runner 才能 emit。

机制上，工具写进 :attr:`~langchain.tools.ToolRuntime.stream_writer` 的载荷会经 langgraph 的
``stream_mode="custom"`` 流出，由 :func:`~evaharness.agents.lead_agent.runner.run_lead_agent`
映射成事件。注意这条通道**只在主 Agent 路径上通**：子 agent 走 ``ainvoke``、没有 custom
模式，``stream_writer`` 是 no-op——写进去的东西会**静默消失，不抛异常**（见 ADR-0015）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

PRESENT_FILES_KIND = "files_presented"
"""载荷的判别键。取值与 :class:`~evaharness.agents.plan_exec.events.FilesPresentedEvent`
的 ``type`` 相同——同一条事件在链路上的两个阶段用同一个名字，少一处对不上的地方。"""


def present_files_payload(files: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """打包一个「展示这些文件」的载荷。

    只描述**展示什么**：不含泳道、不含 ``RunEvent`` 的形状。载荷全部是 JSON 原语，所以
    SSE 与 RunJournal 都能直接吃。
    """
    return {"kind": PRESENT_FILES_KIND, "files": [dict(file) for file in files]}


__all__ = ["PRESENT_FILES_KIND", "present_files_payload"]
