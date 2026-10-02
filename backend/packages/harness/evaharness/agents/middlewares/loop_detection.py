"""循环检测中间件：识别并打破重复的工具调用循环。

简化自 DeerFlow：只保留 hash 层（相同工具调用集合重复出现），去掉按工具
类型计频层。检测策略 ——
- 每次模型响应后 hash 其工具调用（name + 稳定参数键）；
- 滑动窗口内同一 hash 出现 >= warn_threshold 次 → 注入"别再调工具"警告
  （延迟到下一次模型调用，作为追加的 HumanMessage，避免破坏 tool 配对）；
- 出现 >= hard_limit 次 → 剥掉 tool_calls，强制产出文本最终回复。

追踪状态按**检查点坐标**分桶（:func:`~evaharness.agents.middlewares.context.thread_bucket`）。
**桶在每次运行开始时清空**（``before_agent``）——坐标可能跨轮复用（lead agent 就是），
而「循环」是一次运行内的现象，不是跨轮的。不重置的话，同一个工具在第 3 轮会被误判、
第 5 轮直接被掐断。

分桶键必须认**两种上下文形状**（dict 与带 ``thread_id`` 的对象），理由见那个帮手模块。
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

from evaharness.agents.middlewares.context import thread_bucket

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

    def consume_stop_reason(self, thread_id: str) -> str | None:
        """取出并返回某线程触发的硬停原因（loop_capped）。

        **今天只有测试在用它**——生产路径上没有任何消费者，而中间件实例在 T9 之后随请求
        消失（ADR-0011），app 层更拿不到它。真实的可见信号是日志里那行 ``logger.error``
        与「``tool_calls`` 被剥掉、模型被迫产出文本」这个行为。

        曾经还有一条「写进 ``runtime.context['stop_reason']``」的旁路，T9 删掉了：主路径
        传的是 ``LeadContext``（永远进不去那个 dict 分支），而它进得去的时候是在**改写
        调用方给的对象**——把跨请求写种回中间件的路子，正是 T9 要消灭的。
        """
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

        thread_id = thread_bucket(runtime)
        call_hash = _hash_tool_calls(tool_calls)

        history = self._history.setdefault(thread_id, [])
        history.append(call_hash)
        if len(history) > self.window_size:
            history[:] = history[-self.window_size :]

        count = history.count(call_hash)

        if count >= self.hard_limit:
            self._stop_reason[thread_id] = "loop_capped"
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
        warnings = self._pending_warnings.pop(thread_bucket(request.runtime), [])
        if not warnings:
            return handler(request)
        reminder = HumanMessage(content="\n\n".join(dict.fromkeys(warnings)), name="loop_warning")
        return handler(request.override(messages=[*request.messages, reminder]))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        warnings = self._pending_warnings.pop(thread_bucket(request.runtime), [])
        if not warnings:
            return await handler(request)
        reminder = HumanMessage(content="\n\n".join(dict.fromkeys(warnings)), name="loop_warning")
        return await handler(request.override(messages=[*request.messages, reminder]))

    def before_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        """每次运行开始时清掉本线程的桶。

        **桶是按 thread_id 分的，而 thread_id 可能跨轮复用**——lead agent 的坐标就是
        会话级的（``{会话}:lead``）。少了这一步，「查一下时间」在第 3 轮就会撞上
        ``warn_threshold``、第 5 轮直接被剥掉 ``tool_calls`` 强行掐断，用户正常的
        重复提问会莫名其妙地答不出来。

        语义上这也对：**循环是一次运行内的现象，不是跨轮的**。与
        ``TerminalResponseMiddleware`` 在 ``before_agent`` 重置重试预算是同一个先例。
        """
        self.reset(thread_bucket(runtime))
        return None

    async def abefore_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self.before_agent(state, runtime)

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
