"""Typed configuration schema (pydantic).

M1 defines the ``models`` section, T2 adds ``checkpoint``; later milestones extend
:class:`AppConfig` with sandbox / memory / tracing sections.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelConfig(BaseModel):
    """A single chat-model profile.

    ``use`` is a ``"module.path:ClassName"`` string resolved by
    :func:`evaharness.reflection.resolve_class`. Extra keys (e.g. ``max_tokens``)
    are allowed and forwarded to the model constructor.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    use: str = "langchain_openai:ChatOpenAI"
    model: str
    base_url: str | None = None
    api_key: str | None = None
    temperature: float = 0.0


class CheckpointConfig(BaseModel):
    """Checkpoint 持久化（SQLite）配置。

    ``retention_days <= 0`` 表示不做清理（保留全部）——配置写错时倾向于留数据。
    """

    model_config = ConfigDict(extra="allow")

    db_path: str = "./data/checkpoints.sqlite"
    retention_days: int = 7


class AppConfig(BaseModel):
    """Top-level application configuration (loaded from config.yaml)."""

    model_config = ConfigDict(extra="allow")

    config_version: int
    log_level: str = "info"
    models: list[ModelConfig]
    checkpoint: CheckpointConfig = CheckpointConfig()

    def get_model_config(self, name: str) -> ModelConfig | None:
        """Return the model profile named *name*, or ``None``."""
        for model in self.models:
            if model.name == name:
                return model
        return None
