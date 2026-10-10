"""lead agent：EvaAI 的主 Agent（ReAct），**唯一**的顶层路径。

T8 之前顶层是 Plan-Exec 编排图；T8 起换成这个直接用 ``create_agent`` 编译出来的
ReAct 图，Plan-Exec 退到封存路径，并在 T16 彻底下线（见 ADR-0010 与 ADR-0021）。
"""

from evaharness.agents.lead_agent.agent import (
    DEFAULT_LEAD_PROMPT,
    DEFAULT_SUBAGENT_PROMPT,
    LEAD_LANE,
    TASK_TOOL_NAME,
    LeadContext,
    build_lead_middlewares,
    make_lead_agent,
    make_subagent_graph,
)
from evaharness.agents.lead_agent.custom_events import PRESENT_FILES_KIND, present_files_payload
from evaharness.agents.lead_agent.runner import RunCancellation, run_lead_agent
from evaharness.agents.lead_agent.subagent import make_task_tool

__all__ = [
    "DEFAULT_LEAD_PROMPT",
    "DEFAULT_SUBAGENT_PROMPT",
    "LEAD_LANE",
    "PRESENT_FILES_KIND",
    "TASK_TOOL_NAME",
    "LeadContext",
    "RunCancellation",
    "build_lead_middlewares",
    "make_lead_agent",
    "make_subagent_graph",
    "make_task_tool",
    "present_files_payload",
    "run_lead_agent",
]
