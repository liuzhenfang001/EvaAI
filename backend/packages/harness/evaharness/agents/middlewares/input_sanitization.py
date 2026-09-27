"""输入清洗中间件：中和提示注入标签并包裹用户输入边界。

简化自 DeerFlow：只处理最后一条真实用户消息，命中阻塞标签则 HTML 转义
（不拒绝），再用纯文本边界标记包裹。变换是临时的（wrap_model_call），
不写回 state，且异常时 fail-open（原样放行）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)

# 阻塞标签：框架/注入控制标签。命中即转义，失去结构语义但保持可读。
_BLOCKED_TAG_NAMES: frozenset[str] = frozenset(
    {
        "system-reminder",
        "system_reminder",
        "memory",
        "think",
        "analysis",
        "role",
        "system",
        "instruction",
        "important",
        "override",
        "ignore",
        "prompt",
    }
)

_BLOCKED_TAG_PATTERN = re.compile(
    r"<\s*/?\s*(?:" + "|".join(re.escape(t) for t in sorted(_BLOCKED_TAG_NAMES)) + r")\b[^>]*>?",
    re.IGNORECASE,
)

_USER_INPUT_BEGIN = "--- BEGIN USER INPUT ---"
_USER_INPUT_END = "--- END USER INPUT ---"


def neutralize_untrusted_tags(text: str) -> str:
    """对不可信文本中和注入控制标签（HTML 转义，不拒绝、不包裹）。"""
    if not text.strip():
        return text
    return _BLOCKED_TAG_PATTERN.sub(
        lambda m: m.group(0).replace("<", "&lt;").replace(">", "&gt;"),
        text,
    )


def _sanitize_user_content(text: str) -> str:
    """清洗用户内容：转义阻塞标签后包裹纯文本边界标记。"""
    if not text.strip():
        return text
    text = neutralize_untrusted_tags(text)
    # 幂等：已整体包裹则不再重复包裹
    if text.startswith(_USER_INPUT_BEGIN) and text.endswith(_USER_INPUT_END):
        return text
    return f"{_USER_INPUT_BEGIN}\n{text}\n{_USER_INPUT_END}"


def _is_genuine_user_message(message: object) -> bool:
    """真实用户消息：排除框架注入的隐藏 HumanMessage。"""
    if not isinstance(message, HumanMessage):
        return False
    return not (message.additional_kwargs or {}).get("hide_from_ui")


class InputSanitizationMiddleware(AgentMiddleware[AgentState]):
    """转义用户输入中的注入标签并包裹边界标记。"""

    def _process(self, request: ModelRequest) -> ModelRequest:
        """返回最后一条真实用户消息被清洗后的请求（原请求不变）。"""
        messages = list(request.messages)
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if not _is_genuine_user_message(msg):
                continue
            content = msg.content
            if not isinstance(content, str):
                return request
            processed = _sanitize_user_content(content)
            if processed == content:
                return request
            messages[i] = HumanMessage(
                content=processed,
                id=msg.id,
                name=msg.name,
                additional_kwargs=msg.additional_kwargs,
            )
            return request.override(messages=messages)
        return request

    def _try_process(self, request: ModelRequest) -> ModelRequest:
        """清洗请求；异常时 fail-open 放行原请求。"""
        try:
            return self._process(request)
        except Exception:
            logger.warning("Input sanitization failed; passing original request", exc_info=True)
            return request

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._try_process(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await handler(self._try_process(request))
