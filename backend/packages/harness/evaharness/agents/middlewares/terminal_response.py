"""终止回复中间件：确保工具调用后的回合以可见的最终回复结束。

简化自 DeerFlow：保留核心契约 —— 工具运行结束后若模型返回空回复，
自动重试一次（移除空消息 + 跳回 model 节点 + 注入恢复提示）；重试仍空则
写入可见的兜底文案。每次 run 有且仅有一次重试预算（before_agent 重置）。

预算按**检查点坐标**分桶，桶键取自
:func:`~evaharness.runtime.thread_bucket`（必须认得 dict 与带
``thread_id`` 的对象两种上下文形状，理由见那个模块）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ModelCallResult,
    ModelRequest,
    ModelResponse,
    hook_config,
)
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.runtime import Runtime

from evaharness.runtime import thread_bucket

_RECOVERY_PROMPT = (
    "<system_reminder>\n"
    "Your previous response after the tool execution was empty. Review the tool results "
    "already present in the conversation and provide a concise, user-visible final response. "
    "Do not call another tool unless it is strictly necessary.\n"
    "</system_reminder>"
)

_FALLBACK_CONTENT = (
    "The model completed the tool run but returned no final response, including after one "
    "automatic retry. Please try again or use a different model."
)

_TOOL_CALL_FINISH_REASONS = {"tool_calls", "function_call"}


def _has_visible_content(message: AIMessage) -> bool:
    content = message.content
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str) and block.strip():
                return True
            if isinstance(block, dict) and block.get("type") in {"text", "output_text"}:
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    return True
    return False


def _has_tool_call_intent_or_error(message: AIMessage) -> bool:
    if message.tool_calls or getattr(message, "invalid_tool_calls", None):
        return True
    additional_kwargs = message.additional_kwargs or {}
    if additional_kwargs.get("tool_calls") or additional_kwargs.get("function_call"):
        return True
    response_metadata = message.response_metadata or {}
    return response_metadata.get("finish_reason") in _TOOL_CALL_FINISH_REASONS


def _tool_result_in_current_turn(messages: list[Any]) -> bool:
    latest_user_index = -1
    for index, message in enumerate(messages):
        if not isinstance(message, HumanMessage):
            continue
        if (message.additional_kwargs or {}).get("hide_from_ui"):
            continue
        latest_user_index = index
    if latest_user_index == -1:
        return False
    return any(isinstance(message, ToolMessage) for message in messages[latest_user_index + 1 :])


class TerminalResponseMiddleware(AgentMiddleware[AgentState]):
    """工具回合空回复重试一次，仍空则写入兜底文案。"""

    def __init__(self) -> None:
        super().__init__()
        self._retried: set[str] = set()
        self._pending_recovery: set[str] = set()

    def _apply(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        messages = list(state.get("messages") or [])
        if not messages or not isinstance(messages[-1], AIMessage):
            return None
        last = messages[-1]
        if _has_visible_content(last) or _has_tool_call_intent_or_error(last):
            return None
        if not _tool_result_in_current_turn(messages):
            return None

        thread_id = thread_bucket(runtime)
        if thread_id not in self._retried:
            self._retried.add(thread_id)
            self._pending_recovery.add(thread_id)
            message_updates = [RemoveMessage(id=last.id)] if last.id else []
            return {"messages": message_updates, "jump_to": "model"}

        additional_kwargs = dict(last.additional_kwargs or {})
        additional_kwargs.update(
            {
                "evaharness_error_fallback": True,
                "error_reason": "Model returned an empty terminal response after one retry",
            }
        )
        fallback = last.model_copy(update={"content": _FALLBACK_CONTENT, "additional_kwargs": additional_kwargs})
        return {"messages": [fallback]}

    def _augment_request(self, request: ModelRequest) -> ModelRequest:
        thread_id = thread_bucket(request.runtime)
        if thread_id not in self._pending_recovery:
            return request
        self._pending_recovery.discard(thread_id)
        reminder = HumanMessage(
            content=_RECOVERY_PROMPT,
            name="terminal_response_recovery",
            additional_kwargs={"hide_from_ui": True},
        )
        return request.override(messages=[*request.messages, reminder])

    def before_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        self._retried.discard(thread_bucket(runtime))
        self._pending_recovery.discard(thread_bucket(runtime))
        return None

    async def abefore_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        self._retried.discard(thread_bucket(runtime))
        self._pending_recovery.discard(thread_bucket(runtime))
        return None

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        return self._apply(state, runtime)

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        return self._apply(state, runtime)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._augment_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await handler(self._augment_request(request))
