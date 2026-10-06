"""工具测试的共用替身。

``ToolRuntime`` 是个 dataclass，除了 ``execution_info`` / ``server_info`` 之外都必填，
所以能直接手搓——工具的单元测试不该为了拿一个 ``context`` 去跑一整张图。
"""

from __future__ import annotations

from typing import Any

from langchain.tools import ToolRuntime

from evaharness.agents.lead_agent import LeadContext

WORKSPACE_CONTEXT = LeadContext(session_id="th-1", thread_id="th-1:lead", run_id="run-1")
"""lead agent 与子 agent 的形状（带类型的对象）。"""

PLAN_EXEC_CONTEXT: dict[str, Any] = {"thread_id": "th-1:run-1:t1"}
"""Plan-Exec 执行单元的形状：只有坐标，没有 session_id（封存路径仍要能用文件工具）。"""


def fake_runtime(context: Any, *, tool_call_id: str = "call-1") -> ToolRuntime:
    """造一个够用的 ``ToolRuntime``。"""
    return ToolRuntime(
        state=None,
        context=context,
        config={},
        stream_writer=lambda _chunk: None,
        tool_call_id=tool_call_id,
        store=None,
        tools=[],
    )


__all__ = ["PLAN_EXEC_CONTEXT", "WORKSPACE_CONTEXT", "fake_runtime"]
