"""Typed configuration schema (pydantic).

M1 defines the ``models`` section, T2 adds ``checkpoint``, T3 adds ``journal``,
T8 adds ``agent``; later milestones extend :class:`AppConfig` with sandbox /
memory / tracing sections. New sections are always optional with defaults, so
``config_version`` does not need a bump for an additive change.
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


class JournalConfig(BaseModel):
    """RunJournal 的配置。

    ``store`` 是 ``"module.path:ClassName"`` 类路径（同 :class:`ModelConfig` 的
    ``use``）；``dir`` 之类的构造参数透传给 store。默认落 JSONL 而非内存——运行
    记录不落盘的话，「回放」就无从谈起。

    ``dir`` 缺省为 ``None`` 表示「用 store 自己的默认值」：这样换成不接受该参数的
    实现（如 :class:`~evaharness.journal.stores.MemoryJournalStore`）也不会炸。
    """

    model_config = ConfigDict(extra="allow")

    store: str = "evaharness.journal.stores:JsonlJournalStore"
    flush_threshold: int = 20
    dir: str | None = None


class SummarizationConfig(BaseModel):
    """会话摘要压缩：上下文长了就把早先的消息压成一段摘要。

    ``trigger_tokens`` 是**估算**值（langchain 默认的 ``count_tokens_approximately``），
    不是 provider 报的准确用量，所以取一个离上限有安全距离的数：DeepSeek 上下文 64K，
    这里默认 48000。``keep_messages`` 是「尾部保留多少条原样不动」。

    ``model`` 是**模型 profile 的名字**（不是实例），缺省 ``None`` 表示复用主模型。
    压缩要额外花一次 LLM 调用，用便宜的小模型是合理选择。
    """

    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    trigger_tokens: int = 48_000
    keep_messages: int = 20
    model: str | None = None


class AgentConfig(BaseModel):
    """Agent 层配置（T8 起：主 Agent 是 ReAct，Plan-Exec 封存）。"""

    model_config = ConfigDict(extra="allow")

    summarization: SummarizationConfig = SummarizationConfig()


class AppConfig(BaseModel):
    """Top-level application configuration (loaded from config.yaml)."""

    model_config = ConfigDict(extra="allow")

    config_version: int
    log_level: str = "info"
    models: list[ModelConfig]
    checkpoint: CheckpointConfig = CheckpointConfig()
    journal: JournalConfig = JournalConfig()
    agent: AgentConfig = AgentConfig()

    def get_model_config(self, name: str) -> ModelConfig | None:
        """Return the model profile named *name*, or ``None``."""
        for model in self.models:
            if model.name == name:
                return model
        return None
