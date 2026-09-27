"""ReAct 执行器装配：包装 langchain create_agent，注入 EvaAI 默认中间件链。

ReAct 循环本身复用 LangChain v1 ``create_agent``（编译为 LangGraph 图），
EvaAI 只负责注入默认中间件链与默认 state schema，不重写循环。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.chat_models import BaseChatModel
from langgraph.graph.state import CompiledStateGraph

from evaharness.agents.middlewares import build_default_middlewares


def make_react_agent(
    model: BaseChatModel,
    tools: Sequence[Any] | None = None,
    *,
    system_prompt: str | None = None,
    middleware: Sequence[AgentMiddleware] | None = None,
    state_schema: type[AgentState] | None = None,
    checkpointer: Any | None = None,
    **kwargs: Any,
) -> CompiledStateGraph:
    """构建 ReAct 执行器。

    Args:
        model: 已实例化的聊天模型（来自 model factory）。
        tools: 工具序列（LangChain BaseTool / Callable / dict）。
        system_prompt: 系统提示词。
        middleware: 中间件链；缺省用 EvaAI 默认链。
        state_schema: 状态 schema；缺省用 LangChain 默认 AgentState。
        checkpointer: 检查点器（同进程 resume 用，如 MemorySaver）。
        **kwargs: 透传给 ``create_agent``。

    Returns:
        编译好的 LangGraph 状态图。
    """
    if middleware is None:
        middleware = build_default_middlewares()
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        middleware=middleware,
        state_schema=state_schema,
        checkpointer=checkpointer,
        **kwargs,
    )


__all__ = ["make_react_agent"]
