"""步数预算中间件：限制一次运行内「带工具调用的模型响应」的总步数，防止无限换着花样跑下去。

loop_detection 只拦**重复调用同一个工具**的循环；一个 agent 每次调用**不同**的工具、
或者反复「想一步就调一次工具」时它拦不住。这里是它的补集：按总步数硬上限，超过就
在下一次模型响应时剥掉 ``tool_calls`` 强制转文本收尾——与 loop_detection 的 hard-stop
同一个手法（共用 :func:`~evaharness.agents.middlewares.forced_stop.strip_tool_calls`），
**不新增**「stopped」终态，run 仍以 ``completed`` 结束。

计数按**检查点坐标**分桶（:func:`~evaharness.runtime.thread_bucket`），
``before_agent`` 重置，理由与 loop_detection 相同：坐标跨轮复用，「步数上限」是一次
运行内的概念，不是跨轮的。
"""

from __future__ import annotations

import logging

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langgraph.runtime import Runtime

from evaharness.agents.middlewares.forced_stop import strip_tool_calls
from evaharness.runtime import thread_bucket

logger = logging.getLogger(__name__)

_DEFAULT_MAX_STEPS = 100
"""构造缺省值：真正的来源是 ``AgentConfig.max_steps``，这里只是直接 ``StepBudgetMiddleware()``
时的兜底——三者必须同值，别让「配了 100、直接 new 却给 20」这种漂移再发生。"""

_STEP_BUDGET_NOTE = (
    "[STEP BUDGET EXCEEDED] You have reached the maximum number of steps for this turn. "
    "Stop calling tools and produce your final answer now, using the results collected so far."
)


class StepBudgetMiddleware(AgentMiddleware[AgentState]):
    """超过步数上限时强制本轮转文本收尾。"""

    def __init__(self, max_steps: int = _DEFAULT_MAX_STEPS) -> None:
        super().__init__()
        self.max_steps = max_steps
        self._steps: dict[str, int] = {}

    def _apply(self, state: AgentState, runtime: Runtime) -> dict | None:
        messages = state.get("messages", [])
        if not messages:
            return None
        last = messages[-1]
        if getattr(last, "type", None) != "ai":
            return None
        if not getattr(last, "tool_calls", None):
            return None  # 没有工具调用 = 已经在收尾，不算步

        thread_id = thread_bucket(runtime)
        steps = self._steps.get(thread_id, 0) + 1
        self._steps[thread_id] = steps

        if steps > self.max_steps:
            logger.warning(
                "Step budget exceeded — forcing final answer (thread=%s, steps=%d)",
                thread_id,
                steps,
            )
            return {"messages": [strip_tool_calls(last, _STEP_BUDGET_NOTE)]}
        return None

    def before_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        self._steps.pop(thread_bucket(runtime), None)
        return None

    async def abefore_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self.before_agent(state, runtime)

    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)


__all__ = ["StepBudgetMiddleware"]
