"""从图的终态里取出「助手最后说了什么」。

放在 ``agents/`` 而不是某个子包里，是因为它跟**哪条路径在跑**无关，只跟 LangGraph
的 state 形状有关（``values`` 流模式给出的快照）。lead agent 与 Plan-Exec 的执行单元
都靠它把终态翻成一句可展示的文本——「一个任务做完了产出什么」与「一轮对话说完了
什么」是同一个问题。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage


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


__all__ = ["final_text"]
