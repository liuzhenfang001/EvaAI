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
    max_steps: int = 100
    """主 Agent 单次运行内「带工具调用的模型响应」步数上限。

    远低于图级 ``recursion_limit``：``create_agent`` 编译时把它设为 9_999（**不是**
    langgraph 的裸默认 25），所以步数上限的优雅收尾（剥掉 ``tool_calls``、模型转文本）
    总是先于那句看不懂的 ``GraphRecursionError`` 触发。见
    :class:`~evaharness.agents.middlewares.step_budget.StepBudgetMiddleware`。
    """


class EmbeddingConfig(BaseModel):
    """嵌入模型的配置。

    默认指向 DashScope 通义的 **OpenAI 兼容端点**——所以走 :class:`OpenAIEmbeddings` 就行，
    不必为它引一个新 SDK（见 ``docs/research/rag-knowledge-base.md`` §3）。

    ``dimensions`` 必须与向量库建表时的维度一致。``batch_size`` 是**每次请求送多少条文本**：
    DashScope v4 实测上限是 10 条/请求，所以默认 10；OpenAI 原生可以大得多。
    """

    model_config = ConfigDict(extra="allow")

    model: str = "text-embedding-v4"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: str | None = None
    dimensions: int = 1024
    batch_size: int = 10


class KnowledgeConfig(BaseModel):
    """RAG 知识库配置。

    ``enabled`` **默认关**：没有嵌入 key 时不该因为一个可选功能让服务起不来（也没有意义）。
    打开它 = 提供了源目录与嵌入 profile，且 ``api_key`` 已解析出来。

    ``source`` / ``persist_dir`` / ``fingerprint_db_path`` 的相对路径按**项目根**解析（与
    ``checkpoint`` / ``journal`` 同一口径）。``persist_dir`` 是 Chroma 的**持久化目录**
    （不是文件）。``fingerprint_mode`` 是 :func:`evaharness.fingerprint.fingerprint` 的模式：
    ``"mtime"`` 快、``"content"`` 准。
    """

    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    source: str = "./knowledge"
    persist_dir: str = "./data/knowledge"
    fingerprint_db_path: str = "./data/fingerprints.sqlite"
    embedding: EmbeddingConfig = EmbeddingConfig()
    chunk_size: int = 800
    chunk_overlap: int = 120
    top_k: int = 4
    fingerprint_mode: str = "mtime"
    ignore: list[str] = []


class MemoryConfig(BaseModel):
    """长期记忆配置。

    ``enabled`` **默认关**：它会给 lead 链多挂两个中间件、给模型多一件检索工具、每个会话
    多一段注入、并起一个后台抽取队列。默认关是「没打开这个功能的部署，行为与从前逐字节
    相同」的前提（与 ``KnowledgeConfig.enabled`` 同一条理由）。

    ``max_injection_tokens`` 是**整段注入的预算**（画像 + 事实），``guaranteed_categories`` 里
    的分类先占 ``guaranteed_token_budget`` 那条保底通道——记错的事会再犯，所以 ``correction``
    默认享有一条不该被挤掉的通道。

    **抽取是后台的**：``debounce_seconds`` 是「最后一轮对话之后安静多久才抽」，``model`` 是
    抽取用的模型 profile（``None`` = 主模型），``max_facts_in_prompt`` 是每次抽取时把多少条
    现有事实带给模型（它要据此决定替换/删除）。``min_capture_chars`` 以下的消息视为寒暄。

    ``db_path`` 的相对路径按**项目根**解析（与 ``checkpoint`` / ``journal`` 同一口径）。
    """

    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    db_path: str = "./data/memory.sqlite"
    max_injection_tokens: int = 2000
    guaranteed_categories: list[str] = ["correction"]
    guaranteed_token_budget: int = 500
    debounce_seconds: float = 30.0
    model: str | None = None
    extraction_timeout_seconds: float = 60.0
    max_facts_in_prompt: int = 100
    max_capture_turns: int = 20
    min_capture_chars: int = 3


class ToolConfig(BaseModel):
    """一个工具的注册项。

    ``use`` 是 ``"module.path:attr"``，指向**两种东西之一**：

    - 一个 ``BaseTool`` **实例**（``@tool`` 装饰过的函数就是）；
    - 一个**工厂函数**——它吃下面那些额外键作为关键字参数，返回 ``BaseTool``。

    「实例或工厂」这条二选一是为了让 `extra` 有明确归宿：写错的键会让工厂抛
    ``TypeError``，而不是被静默忽略。这样每个工具的参数都在**构建期**绑进闭包，
    工具实例因此是自足的——不需要（也不允许有）任何「读当前配置」的进程全局。

    ``name`` 必须与解析出来的工具自身的 ``.name`` 一致，且全表不许重名：前者决定
    模型看到的名字、后者决定路由落到哪里，两者分歧时症状都是「工具明明在列表里却
    调不动」。这两种错都在**启动期**直接失败。
    """

    model_config = ConfigDict(extra="allow")

    name: str
    use: str


class AppConfig(BaseModel):
    """Top-level application configuration (loaded from config.yaml)."""

    model_config = ConfigDict(extra="allow")

    config_version: int
    log_level: str = "info"
    models: list[ModelConfig]
    checkpoint: CheckpointConfig = CheckpointConfig()
    journal: JournalConfig = JournalConfig()
    agent: AgentConfig = AgentConfig()
    knowledge: KnowledgeConfig = KnowledgeConfig()
    memory: MemoryConfig = MemoryConfig()
    tools: list[ToolConfig] = []
    """工具注册表。**缺省是空表，而且空表会被拒绝启动**（见 app 层的 ``build_tools``）。

    刻意不像 :class:`JournalConfig` 那样给一个「缺省即用内置默认集」的兜底：那会让
    「哪些工具在跑」有两个来源（代码常量 + 配置），而其中一个是隐形的。工具是 agent
    的能力边界，应该一眼能从配置里读出来——代价是漏配这一段的配置**起不来**，
    这是有意的（比「服务正常、助手却什么都不会」好查得多）。
    """

    def get_model_config(self, name: str) -> ModelConfig | None:
        """Return the model profile named *name*, or ``None``."""
        for model in self.models:
            if model.name == name:
                return model
        return None
