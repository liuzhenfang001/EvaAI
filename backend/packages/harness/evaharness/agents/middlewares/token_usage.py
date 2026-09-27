"""用量日志中间件：记录模型响应的 token 用量。

简化自 DeerFlow：去掉 todo/subagent 步级归因，只保留最核心的可观测性 ——
模型响应携带 usage_metadata 时打一条 info 日志，供日志与后续 RunJournal 汇总。
"""

from __future__ import annotations

import logging

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)


class TokenUsageMiddleware(AgentMiddleware[AgentState]):
    """记录模型响应的 token 用量。"""

    def _apply(self, state: AgentState) -> dict | None:
        messages = state.get("messages", [])
        if not messages or not isinstance(messages[-1], AIMessage):
            return None
        usage = getattr(messages[-1], "usage_metadata", None)
        if usage:
            logger.info(
                "LLM token usage: input=%s output=%s total=%s",
                usage.get("input_tokens", "?"),
                usage.get("output_tokens", "?"),
                usage.get("total_tokens", "?"),
            )
        return None

    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state)

    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state)
