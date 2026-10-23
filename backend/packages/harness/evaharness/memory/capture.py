"""从 agent state 里截出「值得抽取」的对话轮次。

这是被动抽取的第一道工序：**只认真实用户消息 + 最终助手回复**。工具调用、工具结果、
隐藏注入消息（``hide_from_ui``）一概不进 transcript——它们要么是内部过程，要么根本
不是对话内容。子 agent 的消息本来就不会进 lead 的 state（它跑在自己的图里），所以这里
不需要单独识别。

**为什么按 watermark 扫一整段窗口，而不是只取最后一轮**：
:class:`~evaharness.memory.update.MemoryUpdater` 抽取失败时不推进水位线，下一次 capture
必须能把失败那一轮一起带上；关机丢弃未决项同理（丢弃的是「这次没做」，不是「永远不做」）。
只取最后一轮会让这两种情况永久丢内容。

**watermark 是身份，不是游标**：``after_id`` 是上一次成功抽取的**最后一条 AI 消息 id**，
它的存在只用来在 state 里定位下标。找不到它（被摘要压缩删掉、或换了一段会话）时**只回退到
最后一个完整轮次**，绝不整段重扫——否则每次摘要后都要把整段历史重抽一遍。

**寒暄过滤只作用于用户话术**：``好的`` / ``谢谢`` / ``ok`` 这类不会成为长期记忆；但哪怕整轮
都被过滤掉，``last_complete_ai_id`` 仍要推进，否则每次 capture 都会把同一段寒暄重新捞一遍。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

DEFAULT_MIN_CAPTURE_CHARS = 3
"""用户消息短于这个长度就当作寒暄/应答，不生成 transcript。"""

_PLEASANTRIES = frozenset(
    {
        "好",
        "好的",
        "好吧",
        "行",
        "可以",
        "嗯",
        "嗯嗯",
        "哦",
        "收到",
        "明白",
        "明白了",
        "了解",
        "了解了",
        "谢谢",
        "谢谢你",
        "多谢",
        "不客气",
        "你好",
        "在吗",
        "ok",
        "okay",
        "thanks",
        "thank you",
        "hi",
        "hello",
        "yes",
        "no",
    }
)

_TOOL_FINISH_REASONS = frozenset({"tool_calls", "function_call"})


@dataclass(frozen=True)
class CapturedTurn:
    """一轮完整对话：用户说了什么、助手最终回了什么。"""

    user_text: str
    assistant_text: str
    last_message_id: str
    """这一轮最终 AI 消息的 id。抽取成功后它就是新的水位线。"""


@dataclass(frozen=True)
class CaptureResult:
    """一次 capture 的结果。"""

    turns: tuple[CapturedTurn, ...] = ()
    last_complete_ai_id: str | None = None
    """窗口里最后一个**完整轮次**的 AI id（哪怕它被寒暄过滤掉）。

    它是水位线能推进到哪里的上界：有用户消息但没等到助手回复的轮次（取消、失败）不算完成，
    水位线不能越过它。
    """

    @property
    def empty(self) -> bool:
        return not self.turns


def capture_turns(
    messages: Sequence[BaseMessage] | None,
    *,
    after_id: str | None = None,
    min_chars: int = DEFAULT_MIN_CAPTURE_CHARS,
    max_turns: int | None = None,
) -> CaptureResult:
    """从 state 消息里截出 ``after_id`` 之后的完整轮次。

    Args:
        messages: ``state["messages"]``。
        after_id: 上一次成功抽取的 AI id；``None`` 表示从头扫。
        min_chars: 用户消息短于它视为寒暄。
        max_turns: 只保留最后这么多轮（水位线落后太远时给 prompt 封顶）。
    """
    window = _window(messages or (), after_id)
    turns: list[CapturedTurn] = []
    last_complete_ai_id: str | None = None
    current_user: str | None = None
    final_ai: AIMessage | None = None

    def emit() -> None:
        nonlocal current_user, final_ai, last_complete_ai_id
        if current_user is None or final_ai is None:
            return
        last_complete_ai_id = final_ai.id
        if _is_substantive(current_user, min_chars):
            turns.append(
                CapturedTurn(
                    user_text=current_user,
                    assistant_text=content_text(final_ai.content).strip(),
                    last_message_id=final_ai.id or "",
                )
            )
        current_user = None
        final_ai = None

    for message in window:
        if _is_genuine_human(message):
            emit()
            current_user = content_text(message.content).strip()
        elif current_user is not None and _is_final_ai(message):
            final_ai = message
    emit()

    if max_turns is not None and max_turns > 0 and len(turns) > max_turns:
        turns = turns[-max_turns:]
    return CaptureResult(turns=tuple(turns), last_complete_ai_id=last_complete_ai_id)


def content_text(content: Any) -> str:
    """把消息内容摊平成纯文本（兼容 ``str`` 与 content blocks 两种形状）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in {"text", "output_text"}:
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _window(messages: Sequence[BaseMessage], after_id: str | None) -> Sequence[BaseMessage]:
    """取 watermark 之后的消息；认不出 watermark 就只回退到最后一个完整轮次。"""
    if not after_id:
        return messages
    for index, message in enumerate(messages):
        if getattr(message, "id", None) == after_id:
            return messages[index + 1 :]
    return _last_turn(messages)


def _last_turn(messages: Sequence[BaseMessage]) -> Sequence[BaseMessage]:
    """从后往前找最后一个真实用户消息，只保留它之后的部分。"""
    for index in range(len(messages) - 1, -1, -1):
        if _is_genuine_human(messages[index]):
            return messages[index:]
    return ()


def _is_genuine_human(message: Any) -> bool:
    """真实用户消息：排除框架注入的隐藏 HumanMessage（记忆块、恢复提示等）。"""
    if not isinstance(message, HumanMessage):
        return False
    return not (message.additional_kwargs or {}).get("hide_from_ui")


def _is_final_ai(message: Any) -> bool:
    """最终助手回复：有可见正文、没有工具调用意图、不是兜底错误文案。"""
    if not isinstance(message, AIMessage):
        return False
    if message.tool_calls or getattr(message, "invalid_tool_calls", None):
        return False
    additional = message.additional_kwargs or {}
    if additional.get("tool_calls") or additional.get("function_call"):
        return False
    if additional.get("evaharness_error_fallback"):
        return False
    if (message.response_metadata or {}).get("finish_reason") in _TOOL_FINISH_REASONS:
        return False
    return bool(content_text(message.content).strip())


def _is_substantive(text: str, min_chars: int) -> bool:
    """有信息量的用户消息：不是纯寒暄，也不短于阈值。"""
    compact = text.strip().strip("。.!！?？,，~～ ")
    if not compact:
        return False
    if compact.lower() in _PLEASANTRIES:
        return False
    return len(compact) >= max(1, min_chars)


__all__ = [
    "DEFAULT_MIN_CAPTURE_CHARS",
    "CaptureResult",
    "CapturedTurn",
    "capture_turns",
    "content_text",
]
