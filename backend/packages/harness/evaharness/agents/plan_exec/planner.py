"""planner：把用户请求拆成结构化计划（任务 + 依赖 + 完成标准）。

``method`` 默认 ``"function_calling"`` 而非 ``langchain-openai`` 的默认值
``"json_schema"``：后者会发 ``response_format={"type": "json_schema", ...}``，
而 OpenAI 兼容端点（含 DeepSeek）普遍不支持，会直接 HTTP 400。上游的自动降级
只按模型名认 ``gpt-3*`` / ``gpt-4*``，认不出 DeepSeek 这类第三方模型——所以这里
**写死**，不跟着模型名走（也正因如此，换模型名不会影响这条选择）。
"""

from __future__ import annotations

from typing import Any, Protocol

from langchain.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from evaharness.agents.plan_exec.schemas import Plan

DEFAULT_PLANNER_PROMPT = (
    "You break a user request into a short plan of concrete tasks.\n"
    "Rules:\n"
    "- Use ids t1, t2, t3... in execution order.\n"
    "- depends_on may only reference ids declared earlier in the list; use [] when independent.\n"
    "- Give each task 1-3 checkable completion criteria.\n"
    "- Keep the plan as small as possible. A simple request is ONE task; never split for the sake of it."
)


class Planner(Protocol):
    """产出任务图的角色。"""

    async def __call__(self, request: str, /) -> Plan: ...


class ModelPlanner:
    """基于模型结构化输出的 planner。

    ``runnable`` 是 ``with_structured_output`` 的产物，公开出来是为了让绑定方式
    可被回归测试检查（见 ``tests/test_plan_exec_agents.py``）。
    """

    def __init__(
        self,
        model: BaseChatModel,
        *,
        system_prompt: str | None = None,
        method: str = "function_calling",
    ) -> None:
        self.model = model
        self.method = method
        # None = 未指定（用默认提示词）；"" = 显式不要系统消息。
        self.system_prompt = DEFAULT_PLANNER_PROMPT if system_prompt is None else system_prompt
        self.runnable = model.with_structured_output(Plan, method=method)

    def _messages(self, request: str) -> list[Any]:
        messages: list[Any] = []
        if self.system_prompt:
            messages.append(SystemMessage(content=self.system_prompt))
        messages.append(HumanMessage(content=request))
        return messages

    async def __call__(self, request: str, /) -> Plan:
        return await self.runnable.ainvoke(self._messages(request))


def make_planner(
    model: BaseChatModel,
    *,
    system_prompt: str | None = None,
    method: str = "function_calling",
) -> Planner:
    """构建 planner。"""
    return ModelPlanner(model, system_prompt=system_prompt, method=method)


__all__ = ["DEFAULT_PLANNER_PROMPT", "ModelPlanner", "Planner", "make_planner"]
