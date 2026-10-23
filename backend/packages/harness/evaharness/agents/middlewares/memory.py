"""长期记忆的两端：**开头注入一次、结尾采集一次**。

T20 的实现是「每次模型调用都在请求层 ``override`` 追加一条记忆消息」。那有两个问题：注入
不落 state，所以每个请求、每个 ReAct 步骤都要重放一遍；而且它让模型每轮都看见一份「记忆
上下文」，更像是在提醒它「你在操作记忆」。T21 改成：

- :class:`MemoryMiddleware` —— ``before_agent`` 里把 ``<memory>`` 段**写进 state**（带
  ``hide_from_ui``），thread 内只出现一次；之后每轮模型都能在历史里看到它。
- :class:`MemoryCaptureMiddleware` —— ``after_agent`` 里把「本轮用户消息 + 最终助手回复」
  交给 app 层的采集闭包，由它去重、进队列、异步抽取。中间件只负责「什么时候」，不认识
  store、队列或 LLM。

**首轮判定用 marker，不用「有没有 AIMessage」**：空库时第一轮本来就没有可注入的内容；
如果那时就认定「已经轮过首轮」，等记忆真正产生后这个会话永远看不到它。改成「state 里没有
记忆 marker 且 render() 非空」后，注入会发生在**第一次有记忆的那一轮**，且此后不再重复。

**摘要压缩是唯一会让它再出现一次的路径**：``SummarizationMiddleware`` 会把老消息整段删掉，
marker 随之消失——下一轮 ``before_agent`` 会补注一次。这是修复（模型确实已经看不到它了），
不是重复注入；严格意义上的「thread 内一次」与「压缩后记忆不丢」只能二选一，我们选后者。

**注入消息用确定性 id**（``memory-injection:<session_id>``）：同一会话并发两个 run 时，
``add_messages`` 按 id 是替换而不是追加，最多留下一条。

**采集失败必须 fail-open**：记忆是后台增益，不该让用户的这一轮对话挂掉或延迟收尾。

**只挂 lead 链**：子 agent 的存在理由就是上下文隔离，把它不知道的用户画像喂进去没有意义；
它的对话也不会被采集（trajectory 里没有「用户对助手说话」这一层）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.runtime import Runtime

from evaharness.memory.inject import MEMORY_TAG
from evaharness.runtime import session_id

logger = logging.getLogger(__name__)

CaptureFn = Callable[[str, Sequence[BaseMessage]], None]
"""采集接缝：``(session_id, state_messages) -> None``。由 app 层绑进 store 与队列。"""


def _is_memory_message(message: object) -> bool:
    """这条消息是不是本中间件注入的记忆块。"""
    return getattr(message, "name", None) == MEMORY_TAG and bool(
        (getattr(message, "additional_kwargs", None) or {}).get("hide_from_ui")
    )


class MemoryMiddleware(AgentMiddleware[AgentState]):
    """把 ``render`` 产出的记忆段作为**一条隐藏消息写进 state**，thread 内只写一次。

    ``render`` 是**无参可调用**，由调用方（app 层）把存储与预算绑进闭包——中间件因此不认识
    存储，也不 import 记忆包的任何机制，只认「给我一段给模型看的文本」。
    """

    def __init__(self, render: Callable[[], str]) -> None:
        super().__init__()
        self._render = render

    def _update(self, state: AgentState, runtime: Runtime) -> dict | None:
        messages = list(state.get("messages") or [])
        if any(_is_memory_message(message) for message in messages):
            return None
        content = self._render()
        if not content:
            return None
        reminder = HumanMessage(
            content=content,
            name=MEMORY_TAG,
            id=f"memory-injection:{session_id(runtime)}",
            additional_kwargs={"hide_from_ui": True},
        )
        return {"messages": [reminder]}

    def _try_update(self, state: AgentState, runtime: Runtime) -> dict | None:
        """渲染失败则放行（fail-open）：读库出问题不该让整轮对话挂掉。"""
        try:
            return self._update(state, runtime)
        except Exception:
            logger.warning("Long-term memory injection failed; skipping this turn", exc_info=True)
            return None

    def before_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._try_update(state, runtime)

    async def abefore_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._try_update(state, runtime)


class MemoryCaptureMiddleware(AgentMiddleware[AgentState]):
    """本轮结束时把对话交给采集闭包；排队与抽取都在后台，不阻塞收尾。"""

    def __init__(self, capture: CaptureFn) -> None:
        super().__init__()
        self._capture = capture

    def _run_capture(self, state: AgentState, runtime: Runtime) -> None:
        messages = list(state.get("messages") or [])
        if not messages:
            return
        self._capture(session_id(runtime), messages)

    def after_agent(self, state: AgentState, runtime: Runtime) -> None:
        try:
            self._run_capture(state, runtime)
        except Exception:
            logger.warning("Long-term memory capture failed; skipping this turn", exc_info=True)
        return None

    async def aafter_agent(self, state: AgentState, runtime: Runtime) -> None:
        try:
            self._run_capture(state, runtime)
        except Exception:
            logger.warning("Long-term memory capture failed; skipping this turn", exc_info=True)
        return None


__all__ = ["CaptureFn", "MemoryCaptureMiddleware", "MemoryMiddleware"]
