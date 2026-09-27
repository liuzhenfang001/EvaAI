"""LLM 错误处理中间件：可重试错误退避重试，最终失败返回兜底回复。

简化自 DeerFlow：去掉进程级并发限流与熔断器，保留核心契约 ——
- 可重试错误（429/5xx/连接超时/服务繁忙）带退避重试；
- 不可重试错误（鉴权/配额）直接兜底，不空耗重试；
- 重试耗尽后返回用户可见的兜底 AIMessage，运行不崩。
GraphBubbleUp（中断/暂停/恢复）原样透传。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage
from langgraph.errors import GraphBubbleUp

logger = logging.getLogger(__name__)

_RETRIABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
_BUSY_PATTERNS = (
    "server busy",
    "temporarily unavailable",
    "try again later",
    "please retry",
    "please try again",
    "overloaded",
    "rate limit",
    "负载较高",
    "服务繁忙",
    "稍后重试",
)
_QUOTA_PATTERNS = (
    "insufficient_quota",
    "quota",
    "billing",
    "credit",
    "payment",
    "余额不足",
    "超出限额",
    "额度不足",
)
_AUTH_PATTERNS = (
    "authentication",
    "unauthorized",
    "invalid api key",
    "invalid_api_key",
    "permission",
    "forbidden",
    "access denied",
    "未授权",
    "无权",
)

_TRANSIENT_EXC_NAMES = {
    "APITimeoutError",
    "APIConnectionError",
    "InternalServerError",
    "ReadError",
    "RemoteProtocolError",
    "StreamChunkTimeoutError",
}


def _extract_detail(exc: BaseException) -> str:
    detail = str(exc).strip()
    if detail:
        return detail
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message.strip():
        return message.strip()
    return exc.__class__.__name__


def _extract_status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _matches_any(detail: str, patterns: tuple[str, ...]) -> bool:
    return any(pattern in detail for pattern in patterns)


def _classify_error(exc: BaseException) -> tuple[bool, str]:
    """返回 (是否可重试, 原因)。原因 ∈ {transient, busy, quota, auth, generic}。"""
    detail = _extract_detail(exc).lower()
    if _matches_any(detail, _QUOTA_PATTERNS):
        return False, "quota"
    if _matches_any(detail, _AUTH_PATTERNS):
        return False, "auth"
    if exc.__class__.__name__ in _TRANSIENT_EXC_NAMES:
        return True, "transient"
    if isinstance(exc, IndexError):
        return True, "transient"
    if _extract_status_code(exc) in _RETRIABLE_STATUS_CODES:
        return True, "transient"
    if _matches_any(detail, _BUSY_PATTERNS):
        return True, "busy"
    return False, "generic"


def _build_user_message(exc: BaseException, reason: str) -> str:
    if reason == "quota":
        return (
            "The configured LLM provider rejected the request because the account is out of quota "
            "or billing is unavailable. Please fix the provider account and try again."
        )
    if reason == "auth":
        return (
            "The configured LLM provider rejected the request because authentication or access is "
            "invalid. Please check the provider credentials and try again."
        )
    if reason in {"busy", "transient"}:
        return (
            "The configured LLM provider is temporarily unavailable after multiple retries. "
            "Please wait a moment and continue the conversation."
        )
    return f"LLM request failed: {_extract_detail(exc)}"


class LLMErrorHandlingMiddleware(AgentMiddleware[AgentState]):
    """可重试错误退避重试，最终失败返回兜底回复。"""

    retry_max_attempts: int = 3
    retry_base_delay_ms: int = 1000
    retry_cap_delay_ms: int = 8000

    def __init__(
        self,
        *,
        retry_max_attempts: int = 3,
        retry_base_delay_ms: int = 1000,
        retry_cap_delay_ms: int = 8000,
    ) -> None:
        super().__init__()
        self.retry_max_attempts = retry_max_attempts
        self.retry_base_delay_ms = retry_base_delay_ms
        self.retry_cap_delay_ms = retry_cap_delay_ms

    def _build_delay_ms(self, prev_delay_ms: int | None) -> int:
        """去相关抖动退避，避免并发重试同步成波峰。"""
        base = self.retry_base_delay_ms
        cap = self.retry_cap_delay_ms
        if prev_delay_ms is None:
            return random.randint(base, min(cap, base * 2))
        return min(cap, prev_delay_ms * 2 + random.randint(0, base))

    def _build_fallback(self, exc: BaseException, reason: str) -> AIMessage:
        return AIMessage(
            content=_build_user_message(exc, reason),
            additional_kwargs={
                "evaharness_error_fallback": True,
                "error_type": exc.__class__.__name__,
                "error_reason": reason,
                "error_detail": _extract_detail(exc),
            },
        )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        attempt = 1
        prev_delay_ms: int | None = None
        while True:
            try:
                return handler(request)
            except GraphBubbleUp:
                raise
            except Exception as exc:
                retriable, reason = _classify_error(exc)
                if retriable and attempt < self.retry_max_attempts:
                    wait_ms = self._build_delay_ms(prev_delay_ms)
                    prev_delay_ms = wait_ms
                    logger.warning(
                        "Transient LLM error on attempt %d/%d; retrying in %dms: %s",
                        attempt,
                        self.retry_max_attempts,
                        wait_ms,
                        _extract_detail(exc),
                    )
                    time.sleep(wait_ms / 1000)
                    attempt += 1
                    continue
                logger.warning(
                    "LLM call failed after %d attempt(s): %s",
                    attempt,
                    _extract_detail(exc),
                    exc_info=exc,
                )
                return self._build_fallback(exc, reason)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        attempt = 1
        prev_delay_ms: int | None = None
        while True:
            try:
                return await handler(request)
            except GraphBubbleUp:
                raise
            except Exception as exc:
                retriable, reason = _classify_error(exc)
                if retriable and attempt < self.retry_max_attempts:
                    wait_ms = self._build_delay_ms(prev_delay_ms)
                    prev_delay_ms = wait_ms
                    logger.warning(
                        "Transient LLM error on attempt %d/%d; retrying in %dms: %s",
                        attempt,
                        self.retry_max_attempts,
                        wait_ms,
                        _extract_detail(exc),
                    )
                    await asyncio.sleep(wait_ms / 1000)
                    attempt += 1
                    continue
                logger.warning(
                    "LLM call failed after %d attempt(s): %s",
                    attempt,
                    _extract_detail(exc),
                    exc_info=exc,
                )
                return self._build_fallback(exc, reason)
