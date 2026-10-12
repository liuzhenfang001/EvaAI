"""lead agent：EvaAI 的主 Agent（ReAct），**唯一**的顶层路径。

T8 之前顶层是 Plan-Exec 编排图；T8 起换成这个直接用 ``create_agent`` 编译出来的
ReAct 图，Plan-Exec 退到封存路径，并在 T16 彻底下线（见 ADR-0010 与 ADR-0021）。

**这一层只剩「怎么把图装起来」**：运行入口、事件契约、运行上下文都在
:mod:`evaharness.runtime`（T19 搬过去，见 ADR-0024）。
"""

from evaharness.agents.lead_agent.agent import (
    DEFAULT_LEAD_PROMPT,
    DEFAULT_SUBAGENT_PROMPT,
    TASK_TOOL_NAME,
    build_lead_middlewares,
    make_lead_agent,
    make_subagent_graph,
)
from evaharness.agents.lead_agent.subagent import make_task_tool

__all__ = [
    "DEFAULT_LEAD_PROMPT",
    "DEFAULT_SUBAGENT_PROMPT",
    "TASK_TOOL_NAME",
    "build_lead_middlewares",
    "make_lead_agent",
    "make_subagent_graph",
    "make_task_tool",
]
