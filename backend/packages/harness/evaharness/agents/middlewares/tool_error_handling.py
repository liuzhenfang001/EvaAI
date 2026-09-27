"""工具错误处理中间件：把工具异常转成 error ToolMessage，让运行继续。

简化自 DeerFlow：去掉 skill/task 归因元数据，只保留核心兜底 —— 捕获工具
执行异常，返回带 tool_call_id 的 error ToolMessage，模型可据此继续或收尾。
GraphBubbleUp（中断/暂停/恢复）原样透传。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

logger = logging.getLogger(__name__)

_MISSING_TOOL_CALL_ID = "missing_tool_call_id"
_RECOVERY_HINT = "Continue with available context, or choose an alternative tool."


class ToolErrorHandlingMiddleware(AgentMiddleware[AgentState]):
    """把工具异常转成 error ToolMessage。"""

    def _build_error_message(self, request: ToolCallRequest, exc: Exception) -> ToolMessage:
        tool_name = str(request.tool_call.get("name") or "unknown_tool")
        tool_call_id = str(request.tool_call.get("id") or _MISSING_TOOL_CALL_ID)
        detail = str(exc).strip() or exc.__class__.__name__
        if len(detail) > 500:
            detail = detail[:497] + "..."
        return ToolMessage(
            content=f"Error: Tool '{tool_name}' failed with {exc.__class__.__name__}: {detail}. {_RECOVERY_HINT}",
            tool_call_id=tool_call_id,
            name=tool_name,
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        try:
            return handler(request)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            logger.exception(
                "Tool execution failed: name=%s id=%s",
                request.tool_call.get("name"),
                request.tool_call.get("id"),
            )
            return self._build_error_message(request, exc)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        try:
            return await handler(request)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            logger.exception(
                "Tool execution failed (async): name=%s id=%s",
                request.tool_call.get("name"),
                request.tool_call.get("id"),
            )
            return self._build_error_message(request, exc)
