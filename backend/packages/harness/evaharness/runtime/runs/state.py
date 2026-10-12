"""读图的终态：这一轮助手说了什么、以及有没有一次没人应答的挂起。

两个函数都是「从最后一次 ``values`` 快照里读东西」，所以同住一处：前者要一句给用户看的
正文，后者要一句给用户看的**失败原因**。它们跟**哪条路径在跑**无关，只跟 LangGraph 的
state 形状有关。

.. note::
   ``final_text`` 原住在 ``agents/messages.py``（它的 docstring 当年就写着「放在这里是因为
   它跟哪条路径在跑无关」——T19 顺着这句话把它搬进了 ``runtime/runs/``，见 ADR-0024）。
   挂起探测原先私藏在 ``runner.py`` 里。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)


def final_text(state: Mapping[str, Any] | None) -> str | None:
    """从图终态里取最后一条有可见文本的 AIMessage。

    从后往前找，跳过没有文本的 AI 消息（纯工具调用）与非 AI 消息。都没有就返回
    ``None``——这不是错误，而是「这一轮没有可展示的正文」。
    """
    if not state:
        return None
    messages = state.get("messages") or []
    for message in reversed(list(messages)):
        if not isinstance(message, AIMessage):
            continue
        text = _message_text(message.content)
        if text:
            return text
    return None


def _message_text(content: Any) -> str:
    """把 AIMessage 的 content（字符串或内容块列表）拍平成纯文本。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"]
        return "".join(parts).strip()
    return ""


_GENERIC_INTERRUPT = (
    "这一轮被 interrupt() 挂起，而没有任何东西会去应答它，所以无法继续。"
    "（EvaAI 目前不实现 LangGraph 的中断/恢复，见 ADR-0017）"
)


def pending_interrupt(state: Mapping[str, Any] | None) -> str | None:
    """最后一次 values 快照里有没有**没人应答**的挂起；有就返回一句给人看的话。

    为什么必须处理：``interrupt()`` 不是异常，图完成后 ``__interrupt__`` 就留在 state 里，
    而运行入口的收尾只读 :func:`final_text`——不在这里拦一下，整件事**没有任何痕迹**。

    为什么放在 harness：这是一条**通用**规则（挂起 ≠ 跑完），不依赖具体是谁调的
    ``interrupt()``。只有文案里那点 MCP 细节是尽人事的——认不出来也照样报，只是措辞通用。
    """
    if not isinstance(state, Mapping):
        return None
    interrupts = state.get("__interrupt__")
    if not interrupts:
        return None
    return _describe_interrupt(interrupts)


def _describe_interrupt(interrupts: Any) -> str:
    """把挂起载荷翻译成一句用户看得懂的话。

    ``langchain.mcp`` 的 payload 形状是 ``MCPElicitationInterrupt``
    （``{"type": "mcp_elicitation", "tool_name": ..., "requests": [...]}``，见
    :mod:`langchain.mcp.elicitation`）。认不出来的一律退回通用措辞——**不猜**。
    """
    for item in interrupts if isinstance(interrupts, Sequence) else [interrupts]:
        value = getattr(item, "value", None)
        if not isinstance(value, Mapping) or value.get("type") != "mcp_elicitation":
            continue
        tool = value.get("tool_name") or "某个 MCP 工具"
        asked = [
            request.get("message")
            for request in value.get("requests") or []
            if isinstance(request, Mapping) and request.get("message")
        ]
        question = f"它问的是：「{'；'.join(asked)}」" if asked else ""
        return (
            f"MCP 工具 {tool!r} 要求人工输入才能继续（elicitation），"
            f"而 EvaAI 还没有应答它的链路，所以这一轮到此为止。{question}"
        )
    return _GENERIC_INTERRUPT


__all__ = ["final_text", "pending_interrupt"]
