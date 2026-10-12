"""子 agent 派发：主 Agent 手上的 ``task`` 工具。

子 agent 的存在理由是**上下文隔离**，不是「更快」或「更聪明」：派发出去的那段工作
（大量工具往返、长中间输出）的中间步骤留在子 agent 自己的 thread 里，只有结论作为
工具返回值回到主对话。主对话因此不会被过程淹掉——这正是长任务里最贵的东西。

坐标是**运行级 + 调用级**的（:func:`~evaharness.checkpoint.subagent_thread_id`）：
子 agent 是短工而不是对话，跨轮不累积，与主 Agent 的会话级固定坐标正好相反。身份取自
``runtime.tool_call_id``——天然唯一、可重入，且不需要任何跨运行的状态（自增计数器就得
考虑并发与重启）。
"""

from __future__ import annotations

from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.config import var_child_runnable_config
from langgraph.graph.state import CompiledStateGraph

from evaharness.agents.lead_agent.agent import TASK_TOOL_NAME
from evaharness.checkpoint import subagent_thread_id
from evaharness.runtime import RunContext, final_text

TASK_TOOL_DESCRIPTION = (
    "Dispatch a subagent to finish one self-contained piece of work and return its "
    "conclusion. Use it when doing the work here would flood the conversation with "
    "intermediate steps (many tool calls, long outputs): the subagent's reasoning and tool "
    "calls stay out of this conversation and only the conclusion comes back.\n"
    "The subagent does NOT see this conversation, so the prompt must be self-contained — "
    "state everything it needs. It cannot ask you questions and it does not answer the user "
    "directly; you are responsible for the final answer."
)

NO_OUTPUT_PLACEHOLDER = "（子 agent 没有产出可展示的结论）"
"""子 agent 跑完却没有可见正文时的返回值。

给一句话而不是空串：空串进了 tool message 后模型往往重新派发一次同样的活。
"""


def make_task_tool(subagent: CompiledStateGraph) -> Any:
    """把编译好的子 agent 图包成一个 ``task`` 工具给主 Agent 用。

    工具只暴露一个 ``prompt`` 参数（不像参照项目那样拆出 ``description`` /
    ``subagent_type``）：我们只有一种子 agent，多一个参数就多一处模型填错的地方，
    而它的意图本来就写在 prompt 里。

    子 agent 用 ``ainvoke`` 跑到底再返回——**本片不把它的中间过程流出去**。
    ``runtime.stream_writer`` 是现成的接缝（把子 agent 的 token 写成一条独立泳道），
    但那要动 ``stream_mode`` 与 runner，留到需要时再做。
    """

    @tool(TASK_TOOL_NAME, description=TASK_TOOL_DESCRIPTION)
    async def task(prompt: str, runtime: ToolRuntime[RunContext]) -> str:
        """派发一个子 agent 独立完成一项工作，返回它的结论。"""
        context = runtime.context
        coordinate = subagent_thread_id(context.session_id, context.run_id, str(runtime.tool_call_id))
        state = await _run_isolated(
            subagent,
            {"messages": [HumanMessage(content=prompt)]},
            coordinate=coordinate,
            context=RunContext(
                session_id=context.session_id,
                thread_id=coordinate,
                run_id=context.run_id,
                # 布局是部署级的，原样继承——子 agent 与主 Agent 看的是**同一份文件**。
                sandbox=context.sandbox,
            ),
        )
        return final_text(state) or NO_OUTPUT_PLACEHOLDER

    return task


async def _run_isolated(
    subagent: CompiledStateGraph,
    payload: Any,
    *,
    coordinate: str,
    context: RunContext,
) -> Any:
    """在**干净的配置**里跑子 agent，然后返回它的终态。

    必须先把 :data:`~langgraph.config.var_child_runnable_config` 置空，否则子 agent 会
    继承宿主运行的那一整套 pregel 内部键（``__pregel_read`` / ``__pregel_scratchpad`` /
    ``checkpoint_ns`` / ``__pregel_checkpointer`` ……）——``config`` 是**合并**而不是替换的。

    症状非常难查，而且是**静默**的：子 agent 不会报错，只是「跑一步就停」——它的输入
    进了父图的 channel 而不是自己的，工具调用因此不会被路由，返回值退化成模型产出的
    第一段文本。实测把这一步去掉，子 agent 的模型调用次数从 2 掉到 1、工具一次都没跑，
    而链路上没有任何异常。

    语义上这也对：子 agent 是**独立的一次运行**（自己的 checkpointer、自己的 thread、
    自己的预算），不是父图 pregel 循环的子节点，本来就不该继承那些键。代价是它的
    run 不再挂在父运行的 callback 树上（我们不做 tracing，不依赖这一点）。
    """
    token = var_child_runnable_config.set(None)
    try:
        return await subagent.ainvoke(
            payload,
            config={"configurable": {"thread_id": coordinate}},
            context=context,
        )
    finally:
        var_child_runnable_config.reset(token)


__all__ = ["NO_OUTPUT_PLACEHOLDER", "TASK_TOOL_DESCRIPTION", "make_task_tool"]
