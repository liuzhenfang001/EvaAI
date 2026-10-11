"""lead agent（主 Agent）的图装配。

T8 起 EvaAI 的路径是一个 **ReAct 主 Agent**，不再是 Plan-Exec 编排；那条路径在 T16
彻底下线（见 ADR-0021）。这里的图直接用 LangChain 的
:func:`~langchain.agents.create_agent` 编译成 LangGraph 图。

它带三样 Plan-Exec 的执行单元没有的东西：``context_schema``（子 agent 派发要靠它取运行
身份）、会话级 checkpoint 坐标（记忆的来源）、中间件链里多一道摘要压缩。当年为此没有把两者
共用一个薄封装——差异是真实的，不是参数不同；那个薄封装（``agents.react``）随 Plan-Exec
一起删掉了。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, SummarizationMiddleware
from langchain.chat_models import BaseChatModel
from langgraph.graph.state import CompiledStateGraph

from evaharness.agents.middlewares import StepBudgetMiddleware, build_default_middlewares
from evaharness.config.app_config import SummarizationConfig
from evaharness.sandbox.paths import SandboxLayout

LEAD_LANE = "main"
"""lead agent 在事件流里的泳道 id。

**不能是空串**：前端 ``ensureBubble`` 对空 ``task_id`` 直接 no-op，正文与工具调用行
会被静默丢弃（见 ``runState.ts``）。它与 planner 产生的 ``t1`` / ``t2`` 不会撞——
后者的提示词就要求按 ``t1, t2, t3...`` 命名。
"""

TASK_TOOL_NAME = "task"
"""派发子 agent 的工具名。子 agent 图里出现它就是递归，:func:`make_subagent_graph` 会拦。"""

DEFAULT_LEAD_PROMPT = (
    "You are EvaAI, a locally deployed personal AI assistant. "
    "Answer in the user's language, concisely and accurately. "
    "When you need a fact (the time, a value, the contents of a file), call a tool "
    "instead of guessing. "
    "You remember the earlier turns of this conversation; use them. "
    f"Delegate a self-contained piece of work to a subagent with the `{TASK_TOOL_NAME}` tool "
    "when doing it here would flood the conversation with intermediate steps."
)

DEFAULT_SUBAGENT_PROMPT = (
    "You are a subagent. Complete the single piece of work you are given, using the "
    "available tools when they help. Answer in the same language as the task. "
    "Your reply is handed back verbatim to the agent that dispatched you, so report the "
    "concrete result and skip pleasantries."
)


@dataclass
class LeadContext:
    """图运行期的上下文：中间件与工具靠它拿到「此刻是哪次运行」。

    **会话与 checkpoint 坐标是两个不同的东西**，这里刻意分成两个字段——把它们混成
    一个（T7 之前反复踩过）会导致「检查点写在 A、循环检测记在 B」这类难查的症状：

    - ``session_id``：用户眼里的一段对话（``th-1``）。
    - ``thread_id``：**实际传给 checkpointer 的坐标**（``th-1:lead``）。它必须叫这个名字、
      且必须就是坐标本身——``LoopDetectionMiddleware`` 与 ``TerminalResponseMiddleware``
      读的都是 ``context["thread_id"]``，并按它分桶。
    - ``run_id``：本轮运行的标识。

    子 agent 的图复用同一个类型，只是 ``thread_id`` 换成它自己的运行级坐标、
    ``session_id`` 仍是同一个会话：派发出来的子 agent 属于**会话**，不属于主 Agent
    那个 checkpoint thread。

    第四个字段 ``sandbox`` 是**部署策略**（宿主根 + 挂载表），见
    :class:`~evaharness.sandbox.paths.SandboxLayout`。它**有默认值**：不碰文件的图（中间的
    中间件、纯推理的测试）不必构造它，而用了文件工具却没注入布局的运行会**响亮地失败**
    （:func:`~evaharness.agents.middlewares.context.sandbox_layout` 取不到就抛），
    不是悄悄写进某个猜出来的目录。
    """

    session_id: str
    thread_id: str
    run_id: str
    sandbox: SandboxLayout | None = None


def build_lead_middlewares(
    model: BaseChatModel,
    *,
    summarization: SummarizationConfig | None = None,
    summary_model: BaseChatModel | None = None,
    max_steps: int = 100,
) -> list[AgentMiddleware]:
    """lead agent 的中间件链 = EvaAI 默认链 + 步数预算 + 会话摘要压缩。

    步数预算只接在**主 Agent** 这条链上，不进 :func:`build_default_middlewares`——
    子 agent 图仍由 langgraph 默认 recursion_limit + 循环检测兜住，本片不扩（它的坐标
    是运行级的，跨轮复用预算没有意义）。

    摘要挂在**最后（最内层）**，这是有理由的：它在 ``before_model`` 里改 state，
    排在最后意味着它是最后一道改消息的工序——量到的 token 数就是模型真正会收到的
    数量（``InputSanitizationMiddleware`` 的包裹、``LoopDetectionMiddleware`` 的
    警告注入与 :class:`StepBudgetMiddleware` 的强制收尾都已经生效）。
    """
    chain: list[AgentMiddleware] = list(build_default_middlewares())
    chain.append(StepBudgetMiddleware(max_steps=max_steps))
    if summarization is None or not summarization.enabled:
        return chain

    chain.append(
        SummarizationMiddleware(
            model=summary_model or model,
            trigger=("tokens", summarization.trigger_tokens),
            keep=("messages", summarization.keep_messages),
        )
    )
    return chain


def make_lead_agent(
    model: BaseChatModel,
    tools: Sequence[Any] | None = None,
    *,
    system_prompt: str | None = None,
    middleware: Sequence[AgentMiddleware] | None = None,
    checkpointer: Any | None = None,
    **kwargs: Any,
) -> CompiledStateGraph:
    """构建主 Agent 的 ReAct 图。

    ``middleware=None`` 用 EvaAI 默认链（**不含**摘要——摘要要显式给
    :class:`SummarizationConfig`，因为它会额外花一次 LLM 调用）。

    ``context_schema`` 固定为 :class:`LeadContext`，不接受覆盖：子 agent 派发工具靠它
    取运行身份，换成别的类型会在派发那一刻才炸。
    """
    return create_agent(
        model=model,
        tools=list(tools) if tools else [],
        system_prompt=system_prompt or DEFAULT_LEAD_PROMPT,
        middleware=list(middleware) if middleware is not None else build_default_middlewares(),
        context_schema=LeadContext,
        checkpointer=checkpointer,
        **kwargs,
    )


def make_subagent_graph(
    model: BaseChatModel,
    tools: Sequence[Any] | None = None,
    *,
    system_prompt: str | None = None,
    middleware: Sequence[AgentMiddleware] | None = None,
    checkpointer: Any | None = None,
    **kwargs: Any,
) -> CompiledStateGraph:
    """构建子 agent 的 ReAct 图（独立编译产物，**不含**派发工具）。

    防递归是**结构性的**而不是靠自律：子 agent 的图里根本没有 :data:`TASK_TOOL_NAME`
    这个工具，所以它调不出来；真把那个工具传进来会在这里直接抛，而不是等到运行期
    发现递归把调用次数放大成天文数字。

    子 agent 不挂摘要压缩：它的坐标是运行级的、一次派发一次跑完，没有「长会话」可言。
    """
    for tool in tools or ():
        if getattr(tool, "name", None) == TASK_TOOL_NAME:
            raise ValueError(
                f"子 agent 的图不能带 {TASK_TOOL_NAME!r} 工具——它会派发新的子 agent，"
                "递归没有上界。只用业务工具构建这个图。"
            )
    return create_agent(
        model=model,
        tools=list(tools) if tools else [],
        system_prompt=system_prompt or DEFAULT_SUBAGENT_PROMPT,
        middleware=list(middleware) if middleware is not None else build_default_middlewares(),
        context_schema=LeadContext,
        checkpointer=checkpointer,
        **kwargs,
    )


__all__ = [
    "DEFAULT_LEAD_PROMPT",
    "DEFAULT_SUBAGENT_PROMPT",
    "LEAD_LANE",
    "TASK_TOOL_NAME",
    "LeadContext",
    "build_lead_middlewares",
    "make_lead_agent",
    "make_subagent_graph",
]
