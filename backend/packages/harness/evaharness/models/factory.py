"""Chat-model factory (OpenAI-compatible, DeepSeek default).

Simplified from DeerFlow: reflect the model class from ``use``, filter metadata
fields, inject endpoint/key, instantiate. No thinking/codex/vllm special-casing.
"""

from __future__ import annotations

from langchain.chat_models import BaseChatModel

from evaharness.config.app_config import AppConfig
from evaharness.reflection import resolve_class

# Profile metadata that must never reach the model constructor.
_METADATA_FIELDS = frozenset({"use", "name"})


def create_chat_model(app_config: AppConfig, name: str | None = None, **kwargs) -> BaseChatModel:
    """Create a chat model instance from *app_config*.

    Args:
        app_config: Typed application configuration.
        name: Model profile name; defaults to the first configured profile.
        **kwargs: Per-call overrides layered over the configured profile.

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
    settings.update(kwargs)
    return model_class(**settings)
