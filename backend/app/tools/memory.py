"""长期记忆的**策略**侧：库文件在哪、注入多少、抽取用哪个模型、去抖多久。

机制全在 :mod:`evaharness.memory`（数据形状 / SQLite 存储 / 渲染 / 三件工具 / 采集 / 队列 /
抽取）——这一层只做部署决定，并把几个「无参或低参可调用」交给建图工厂：

- ``render``：读一次快照渲染成一段话（注入中间件在 thread 开头调一次）；
- ``capture``：把本轮对话按水位线截成完整轮次、丢进队列（采集中间件在轮末调一次）；
- ``extractor``：调模型把对话变成结构化更新（队列 worker 在后台调）。

**T21 起写入是被动的**：模型只拿 ``memory_search``，不再有 ``memory_save`` / ``memory_forget``。
三件工具的代码保留（管理页与将来的 tool 模式要用），但 ``LEAD_MEMORY_TOOL_NAMES`` 只放行检索。

**队列在 lifespan 里建**：它要绑事件循环的定时器，还要在关机时被取消。``aclose`` 的顺序是
**先停队列、再关库**——反过来的话，正在飞的抽取会撞上一个已经关掉的连接。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tools import BaseTool

from evaharness.config.app_config import AppConfig, MemoryConfig
from evaharness.memory import (
    LEAD_MEMORY_TOOL_NAMES,
    CaptureResult,
    MemoryQueue,
    MemoryStore,
    MemoryUpdate,
    MemoryUpdater,
    SqliteMemoryStore,
    capture_turns,
    make_memory_tools,
    render_memory_section,
    tolerant_parse_update,
)
from evaharness.memory.capture import content_text
from evaharness.memory.update import Extractor
from evaharness.models.factory import create_chat_model
from evaharness.runtime import RunOptions

logger = logging.getLogger(__name__)

_EXTRACTION_RETRY = (
    "上一次输出不是合法 JSON。请只输出符合 schema 的 JSON 对象，"
    "不要 Markdown 围栏、不要任何解释文字。"
)


@dataclass
class MemoryRuntime:
    """启动期建好的记忆长命物，由 app 的 ``AsyncExitStack`` 持有到关机。"""

    store: MemoryStore
    tools: list[BaseTool] = field(default_factory=list)
    render: Callable[[], str] = lambda: ""
    capture: Callable[[str, Sequence[BaseMessage]], None] = lambda thread_id, messages: None
    queue: MemoryQueue | None = None
    updater: MemoryUpdater | None = None

    async def aclose(self) -> None:
        """收尾：**先停队列，再关库**（见模块说明）。"""
        if self.queue is not None:
            await self.queue.aclose()
        self.store.close()

    async def clear(self) -> None:
        """清空全部记忆，并让飞行中的抽取作废（队列 epoch）。"""
        if self.queue is not None:
            self.queue.clear()
        self.store.clear()


def make_render(store: MemoryStore, config: MemoryConfig) -> Callable[[], str]:
    """把「读快照 → 渲染」封成无参可调用：**每次调用都读最新的**。

    现在它一个会话只被调一次（thread 开头），但**仍然不缓存**：同一时刻可能有另一个会话
    刚完成抽取，缓存会让「新记忆」永远晚一个会话生效。一次小表查询换掉一整类 bug。
    """

    def render() -> str:
        return render_memory_section(
            store.snapshot(),
            max_tokens=config.max_injection_tokens,
            guaranteed_categories=tuple(config.guaranteed_categories),
            guaranteed_token_budget=config.guaranteed_token_budget,
        )

    return render


def make_capture(
    store: MemoryStore,
    queue: MemoryQueue,
    config: MemoryConfig,
) -> Callable[[str, Sequence[BaseMessage]], None]:
    """把「读水位线 → 截轮次 → 入队」封成采集中间件吃的闭包。"""

    def capture(thread_id: str, messages: Sequence[BaseMessage]) -> None:
        result: CaptureResult = capture_turns(
            messages,
            after_id=store.watermark(thread_id),
            min_chars=config.min_capture_chars,
            max_turns=config.max_capture_turns,
        )
        queue.submit(thread_id, result)

    return capture


def make_extractor(app_config: AppConfig, config: MemoryConfig) -> Extractor:
    """建抽取器：主模型（可配 profile）+ ``json_mode`` + 一次容错重试。

    **必须显式 ``method="json_mode"``**：``with_structured_output`` 的默认值是
    ``json_schema``，而 DeepSeek 的 Chat Completions 只认 ``{"type": "json_object"}``，
    默认路径会直接 400。``json_object`` 只保证「是 JSON」不保证结构，所以还要
    :func:`tolerant_parse_update` 兜底。

    抽取是内部辅助调用，固定 ``thinking="off"``：让后台整理跟着用户的思考档位跑没有意义，
    只会让每次抽取都变贵。
    """
    model = create_chat_model(app_config, config.model or None, options=RunOptions(thinking="off"))
    structured = model.with_structured_output(MemoryUpdate, method="json_mode", include_raw=True)

    async def extract(messages: Sequence[BaseMessage]) -> MemoryUpdate:
        parsed = _parse_structured(await structured.ainvoke(messages))
        if parsed is not None:
            return parsed
        parsed = _parse_structured(
            await structured.ainvoke([*messages, HumanMessage(content=_EXTRACTION_RETRY)])
        )
        if parsed is None:
            raise ValueError("抽取模型连续两次没有给出合法的 MemoryUpdate JSON")
        return parsed

    return extract


def _parse_structured(result: object) -> MemoryUpdate | None:
    """``with_structured_output(include_raw=True)`` 的结果 → MemoryUpdate（失败返回 None）。"""
    if not isinstance(result, dict):
        return result if isinstance(result, MemoryUpdate) else None
    parsed = result.get("parsed")
    if isinstance(parsed, MemoryUpdate) and result.get("parsing_error") is None:
        return parsed
    raw = result.get("raw")
    text = content_text(getattr(raw, "content", "")) if raw is not None else ""
    try:
        return tolerant_parse_update(text)
    except ValueError:
        return None


async def start_memory(
    config: MemoryConfig,
    *,
    db_path: Path,
    extractor: Extractor | None = None,
) -> MemoryRuntime | None:
    """按配置把长期记忆建起来；``enabled`` 为假则返回 ``None``。

    ``extractor=None`` 表示「只读 + 检索」：不建队列、不采集，适合测试与诊断。生产 lifespans
    会传入 :func:`make_extractor` 的结果。
    """
    if not config.enabled:
        logger.info("长期记忆未启用（memory.enabled=false），不加载记忆工具")
        return None

    store = SqliteMemoryStore(db_path)
    all_tools = make_memory_tools(store)
    runtime = MemoryRuntime(
        store=store,
        tools=[tool for tool in all_tools if tool.name in LEAD_MEMORY_TOOL_NAMES],
        render=make_render(store, config),
    )
    if extractor is not None:
        updater = MemoryUpdater(
            store,
            extractor,
            timeout=config.extraction_timeout_seconds,
            max_facts=config.max_facts_in_prompt,
        )
        queue = MemoryQueue(updater.update, debounce_seconds=config.debounce_seconds)
        runtime.updater = updater
        runtime.queue = queue
        runtime.capture = make_capture(store, queue, config)
    return runtime


__all__ = [
    "MemoryRuntime",
    "make_capture",
    "make_extractor",
    "make_render",
    "start_memory",
]
