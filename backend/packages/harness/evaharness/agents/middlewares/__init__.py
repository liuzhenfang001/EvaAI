"""EvaAI 硬核中间件：输入清洗 / LLM 错误兜底 / 工具错误兜底 / 用量日志 / 循环检测 / 终止兜底。

长期记忆的**一头一尾**（``MemoryMiddleware`` 会话开头注入、``MemoryCaptureMiddleware`` 轮末采集）
**不在** :func:`build_default_middlewares` 里——它们默认关，只有 ``memory.enabled`` 时才由
:func:`~evaharness.agents.lead_agent.agent.build_lead_middlewares` append 到 lead 链上
（与摘要压缩同一个先例）。
"""

from langchain.agents.middleware import AgentMiddleware

from evaharness.agents.middlewares.input_sanitization import InputSanitizationMiddleware
from evaharness.agents.middlewares.llm_error_handling import LLMErrorHandlingMiddleware
from evaharness.agents.middlewares.loop_detection import LoopDetectionMiddleware
from evaharness.agents.middlewares.memory import MemoryCaptureMiddleware, MemoryMiddleware
from evaharness.agents.middlewares.step_budget import StepBudgetMiddleware
from evaharness.agents.middlewares.terminal_response import TerminalResponseMiddleware
from evaharness.agents.middlewares.token_usage import TokenUsageMiddleware
from evaharness.agents.middlewares.tool_error_handling import ToolErrorHandlingMiddleware


def build_default_middlewares() -> list[AgentMiddleware]:
    """返回 EvaAI 默认中间件链（列表首项 = 最外层）。"""
    return [
        InputSanitizationMiddleware(),
        LLMErrorHandlingMiddleware(),
        ToolErrorHandlingMiddleware(),
        TokenUsageMiddleware(),
        LoopDetectionMiddleware(),
        TerminalResponseMiddleware(),
    ]


__all__ = [
    "InputSanitizationMiddleware",
    "LLMErrorHandlingMiddleware",
    "ToolErrorHandlingMiddleware",
    "TokenUsageMiddleware",
    "LoopDetectionMiddleware",
    "MemoryCaptureMiddleware",
    "MemoryMiddleware",
    "StepBudgetMiddleware",
    "TerminalResponseMiddleware",
    "build_default_middlewares",
]
