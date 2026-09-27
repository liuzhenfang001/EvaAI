"""循环检测中间件：识别并打破重复的工具调用循环。

简化自 DeerFlow：只保留 hash 层（相同工具调用集合重复出现），去掉按工具
类型计频层。检测策略 ——
- 每次模型响应后 hash 其工具调用（name + 稳定参数键）；
- 滑动窗口内同一 hash 出现 >= warn_threshold 次 → 注入"别再调工具"警告
  （延迟到下一次模型调用，作为追加的 HumanMessage，避免破坏 tool 配对）；
- 出现 >= hard_limit 次 → 剥掉 tool_calls，强制产出文本最终回复。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)

_DEFAULT_WARN_THRESHOLD = 3
_DEFAULT_HARD_LIMIT = 5
_DEFAULT_WINDOW_SIZE = 20

_WARNING_MSG = (
    "[LOOP DETECTED] You are repeating the same tool calls. Stop calling tools and "
    "produce your final answer now. If you cannot complete the task, summarize what "
    "you accomplished so far."
)
_HARD_STOP_MSG = (
    "[FORCED STOP] Repeated tool calls exceeded the safety limit. Producing final answer with "
    "results collected so far."
)

_SALIENT_FIELDS = ("path", "url", "query", "command", "pattern", "glob", "cmd")


def _stable_tool_key(name: str, args: dict) -> str:
    """用显著字段派生稳定键，避免对噪声过拟合。"""
    stable = {field: args[field] for field in _SALIENT_FIELDS if args.get(field) is not None}
    return json.dumps(stable or args, sort_keys=True, default=str)


def _hash_tool_calls(tool_calls: list[dict]) -> str:
    """对工具调用集合做顺序无关的确定性 hash。"""
    normalized = sorted(
        f"{tc.get('name', '')}:{_stable_tool_key(tc.get('name', ''), tc.get('args', {}) or {})}"
        for tc in tool_calls
    )
    return hashlib.md5(json.dumps(normalized, sort_keys=True).encode()).hexdigest()[:12]


def _strip_tool_calls(last_msg, note: str):
    """清空 tool_calls 元数据，追加说明文本，强制转成普通文本回复。"""
    content = last_msg.content
    if isinstance(content, str):
        content = f"{content}\n\n{note}" if content else note
    elif isinstance(content, list):
        content = [*content, {"type": "text", "text": note}]
    else:
        content = note
    update: dict = {"tool_calls": [], "content": content}
    additional_kwargs = dict(getattr(last_msg, "additional_kwargs", {}) or {})
    additional_kwargs.pop("tool_calls", None)
    additional_kwargs.pop("function_call", None)
    update["additional_kwargs"] = additional_kwargs
    return last_msg.model_copy(update=update)


class LoopDetectionMiddleware(AgentMiddleware[AgentState]):
    """识别并打破重复的工具调用循环。"""

    def __init__(
        self,
        warn_threshold: int = _DEFAULT_WARN_THRESHOLD,
        hard_limit: int = _DEFAULT_HARD_LIMIT,
        window_size: int = _DEFAULT_WINDOW_SIZE,
    ) -> None:
        super().__init__()
        self.warn_threshold = warn_threshold
        self.hard_limit = hard_limit
        self.window_size = window_size
        self._history: dict[str, list[str]] = {}
        self._warned: dict[str, set[str]] = {}
        self._pending_warnings: dict[str, list[str]] = {}
        self._stop_reason: dict[str, str] = {}

    def _thread_id(self, runtime: Runtime) -> str:
        context = getattr(runtime, "context", None)
        if isinstance(context, dict) and context.get("thread_id"):
            return str(context["thread_id"])
        return "default"

    def consume_stop_reason(self, thread_id: str) -> str | None:
        """取出并返回某线程触发的硬停原因（loop_capped）。"""
        return self._stop_reason.pop(thread_id, None)

    def _apply(self, state: AgentState, runtime: Runtime) -> dict | None:
        messages = state.get("messages", [])
        if not messages:
            return None
        last = messages[-1]
        if getattr(last, "type", None) != "ai":
            return None
        tool_calls = getattr(last, "tool_calls", None)
        if not tool_calls:
            return None

        thread_id = self._thread_id(runtime)
        call_hash = _hash_tool_calls(tool_calls)

        history = self._history.setdefault(thread_id, [])
        history.append(call_hash)
        if len(history) > self.window_size:
            history[:] = history[-self.window_size :]

        count = history.count(call_hash)

        if count >= self.hard_limit:
            self._stop_reason[thread_id] = "loop_capped"
            context = getattr(runtime, "context", None)
            if isinstance(context, dict):
                context["stop_reason"] = "loop_capped"
            logger.error("Loop hard limit reached — forcing stop (thread=%s)", thread_id)
            return {"messages": [_strip_tool_calls(last, _HARD_STOP_MSG)]}

        if count >= self.warn_threshold:
            warned = self._warned.setdefault(thread_id, set())
            if call_hash not in warned:
                warned.add(call_hash)
                logger.warning("Repetitive tool calls detected — injecting warning (thread=%s)", thread_id)
                self._pending_warnings.setdefault(thread_id, []).append(_WARNING_MSG)
        return None

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        warnings = self._pending_warnings.pop(self._thread_id(request.runtime), [])
        if not warnings:
            return handler(request)
        reminder = HumanMessage(content="\n\n".join(dict.fromkeys(warnings)), name="loop_warning")
        return handler(request.override(messages=[*request.messages, reminder]))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        warnings = self._pending_warnings.pop(self._thread_id(request.runtime), [])
        if not warnings:
            return await handler(request)
        reminder = HumanMessage(content="\n\n".join(dict.fromkeys(warnings)), name="loop_warning")
        return await handler(request.override(messages=[*request.messages, reminder]))

    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    def reset(self, thread_id: str | None = None) -> None:
        """清空追踪状态；给定 thread_id 则只清该线程。"""
        if thread_id is None:
            self._history.clear()
            self._warned.clear()
            self._pending_warnings.clear()
            self._stop_reason.clear()
            return
        self._history.pop(thread_id, None)
        self._warned.pop(thread_id, None)
        self._pending_warnings.pop(thread_id, None)
        self._stop_reason.pop(thread_id, None)
