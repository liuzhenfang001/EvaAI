"""Chat-model factory (OpenAI-compatible, DeepSeek default).

Simplified from DeerFlow: reflect the model class from ``use``, filter metadata
fields, inject endpoint/key, instantiate.

「思考」是唯一一处 provider 专属的形状（``extra_body.thinking`` + 顶层
``reasoning_effort``），放在这里而不是散到调用方：见 :func:`thinking_model_kwargs`。
"""

from __future__ import annotations

from typing import Any

from langchain.chat_models import BaseChatModel

from evaharness.config.app_config import AppConfig
from evaharness.reflection import resolve_class
from evaharness.runtime import RunOptions, ThinkingLevel

# Profile metadata that must never reach the model constructor.
_METADATA_FIELDS = frozenset({"use", "name"})


def thinking_model_kwargs(thinking: ThinkingLevel) -> dict[str, Any]:
    """把思考档位翻成模型构造参数（OpenAI 兼容的约定）。

    形状**实测可用**（2026-10-08，DeepSeek）：``thinking.type=disabled`` 时完全不产 reasoning，
    ``enabled`` 时同一道题会真的多出思考、而且答对率不同。

    ``off`` 一并把 ``reasoning_effort`` 压到 ``minimal``：provider 那边的开关已经足够关闭思考，
    这个力度只是让「关了就别分配额度」这件事更明确（SDK 的合法值里有 ``minimal``）。

    **不把 provider 的形状散到调用方**的理由是可组合性：``extra_body`` 在
    :func:`create_chat_model` 里是**整键替换**的，调用方各自去拼就会把 profile 里原有的
    ``extra_body`` 冲掉。
    """
    if thinking == "off":
        return {"extra_body": {"thinking": {"type": "disabled"}}, "reasoning_effort": "minimal"}
    return {"extra_body": {"thinking": {"type": "enabled"}}, "reasoning_effort": thinking}


def _merge_extra_body(settings: dict[str, Any], overrides: dict[str, Any]) -> None:
    """把 ``extra_body`` 按层合并进 *overrides*，而不是让后者整键替换。

    ``settings.update(kwargs)`` 是浅的：profile 里写好的 ``extra_body``（provider 的其它参数）
    会被整个冲掉，症状是「一开思考，之前配的那些参数就没了」。这里只对 ``extra_body`` 这一层
    做合并——它是唯一一个「profile 与运行期都会往里放东西」的字典。

    **改的是 overrides 而不是 settings**：调用方紧接着会 ``settings.update(overrides)``，
    那时 extra_body 这个键还是由 overrides 决定的。往 settings 里写会被那一步覆盖回去。
    """
    base = settings.get("extra_body")
    extra = overrides.get("extra_body")
    if not isinstance(base, dict) or not isinstance(extra, dict):
        return
    merged = {**base, **extra}
    # 再往下一层合一次：profile 可能已经写了 extra_body.thinking 的其它键（例如厂商扩展）。
    if isinstance(base.get("thinking"), dict) and isinstance(extra.get("thinking"), dict):
        merged["thinking"] = {**base["thinking"], **extra["thinking"]}
    overrides["extra_body"] = merged


def create_chat_model(
    app_config: AppConfig,
    name: str | None = None,
    *,
    options: RunOptions | None = None,
    **kwargs,
) -> BaseChatModel:
    """Create a chat model instance from *app_config*.

    Args:
        app_config: Typed application configuration.
        name: Model profile name; defaults to the first configured profile.
        options: Per-run knobs (:class:`~evaharness.runtime.RunOptions`)。``None`` 表示
            「不带任何运行参数」——**行为与老调用方完全一致**。
        **kwargs: Per-call overrides layered over the configured profile.
            **别拿它传 ``timeout`` / ``http_client``**：那类参数参与 httpx 客户端缓存键，
            每请求变会让连接池按请求泛滥（ADR-0011）。

    Returns:
        An instance of the class referenced by the profile's ``use`` field.
    """
    if name is None:
        if not app_config.models:
            raise ValueError("No models configured")
        name = app_config.models[0].name
    model_config = app_config.get_model_config(name)
    if model_config is None:
        raise ValueError(f"Model {name!r} not found in config")
    model_class = resolve_class(model_config.use, BaseChatModel)
    settings = model_config.model_dump(exclude_none=True, exclude=_METADATA_FIELDS)
    if options is not None:
        derived = thinking_model_kwargs(options.thinking)
        _merge_extra_body(settings, derived)
        settings.update(derived)
    settings.update(kwargs)
    return model_class(**settings)


__all__ = ["create_chat_model", "thinking_model_kwargs"]
