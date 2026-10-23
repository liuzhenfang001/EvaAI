"""长期记忆：跨会话留下的画像与事实。

**机制在 harness，策略在 app**（与沙箱、知识库同一条分界）。这一层提供的是通用的机器：

- :mod:`~evaharness.memory.schema` —— 数据形状（六块画像 + 原子事实）；
- :mod:`~evaharness.memory.store` —— SQLite 存储（画像 / 事实 / 水位线）；
- :mod:`~evaharness.memory.inject` —— 把快照渲染成注入给模型的那一段；
- :mod:`~evaharness.memory.capture` —— 从 state 里截出「用户消息 + 最终回复」的完整轮次；
- :mod:`~evaharness.memory.queue` —— 按会话去抖、单 worker 串行的后台队列；
- :mod:`~evaharness.memory.update` —— 抽取 schema / prompt / 容错解析 / 落库 / 水位线；
- :mod:`~evaharness.memory.tool` —— 三件工具（``memory_save`` / ``memory_search`` / ``memory_forget``）。

「库文件在哪、注入多少 token、用哪个模型抽取、去抖多久」都是**部署策略**，由 app 从配置里读了
注入。一进一出两个中间件住在 :mod:`evaharness.agents.middlewares.memory`——它们只吃
``Callable[[], str]`` 与 ``Callable[[str, messages], None]``，不认识存储。

**写入是被动的**：模型只拿 ``memory_search``；对话由采集中间件在轮末交给队列，安静若干秒后
由抽取器整理成画像与事实。三件工具保留在代码里，主要供管理页与将来的 tool 模式使用。
"""

from __future__ import annotations

from evaharness.memory.capture import CapturedTurn, CaptureResult, capture_turns
from evaharness.memory.inject import (
    DEFAULT_GUARANTEED_CATEGORIES,
    DEFAULT_GUARANTEED_TOKEN_BUDGET,
    MEMORY_TAG,
    estimate_tokens,
    render_memory_section,
)
from evaharness.memory.queue import MemoryQueue
from evaharness.memory.schema import (
    DEFAULT_FACT_CATEGORY,
    DEFAULT_FACT_CONFIDENCE,
    FACT_CATEGORIES,
    PROFILE_GROUPS,
    Fact,
    MemorySnapshot,
    ProfileBlock,
    ProfileSection,
    new_fact_id,
)
from evaharness.memory.store import (
    DuplicateFact,
    MemoryError,
    MemoryStore,
    SqliteMemoryStore,
    UnknownFact,
)
from evaharness.memory.tool import (
    LEAD_MEMORY_TOOL_NAMES,
    MEMORY_FORGET_NAME,
    MEMORY_SAVE_NAME,
    MEMORY_SEARCH_NAME,
    MEMORY_TOOL_NAMES,
    make_memory_tools,
)
from evaharness.memory.update import (
    FactAdd,
    FactUpdate,
    MemoryUpdate,
    MemoryUpdater,
    ProfilePatch,
    apply_update,
    build_extraction_messages,
    tolerant_parse_update,
)

__all__ = [
    "DEFAULT_FACT_CATEGORY",
    "DEFAULT_FACT_CONFIDENCE",
    "DEFAULT_GUARANTEED_CATEGORIES",
    "DEFAULT_GUARANTEED_TOKEN_BUDGET",
    "FACT_CATEGORIES",
    "LEAD_MEMORY_TOOL_NAMES",
    "MEMORY_FORGET_NAME",
    "MEMORY_SAVE_NAME",
    "MEMORY_SEARCH_NAME",
    "MEMORY_TAG",
    "MEMORY_TOOL_NAMES",
    "PROFILE_GROUPS",
    "CaptureResult",
    "CapturedTurn",
    "DuplicateFact",
    "Fact",
    "FactAdd",
    "FactUpdate",
    "MemoryError",
    "MemoryQueue",
    "MemorySnapshot",
    "MemoryStore",
    "MemoryUpdate",
    "MemoryUpdater",
    "ProfileBlock",
    "ProfilePatch",
    "ProfileSection",
    "SqliteMemoryStore",
    "UnknownFact",
    "apply_update",
    "build_extraction_messages",
    "capture_turns",
    "estimate_tokens",
    "make_memory_tools",
    "new_fact_id",
    "render_memory_section",
    "tolerant_parse_update",
]
