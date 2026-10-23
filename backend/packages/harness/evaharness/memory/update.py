"""被动抽取的**写入侧**：把一段对话整理成画像更新与事实增删改。

分工与知识库、注入那段一致：这里是纯机制（schema / prompt / 解析 / 落库 / 水位线），
「用哪个模型、去抖多久」是部署策略，在 app 层。

**为什么允许删除与替换**（``facts_update`` / ``facts_remove``）：只追加的话，同一件事说两遍
就会留下两条互相矛盾的记录，注入里越积越乱。抽取器因此会拿到现有事实的 id，可以显式地
「取代旧的」——``status=superseded`` 这个字段就是为这件事留的（本片先用「删掉旧的」实现，
不引入软删除）。

**落库顺序是 remove → update → add → profile**：``content`` 上有唯一索引，先删能释放名字，
先改再增才不会让「改名」撞上自己。每一条都单独 try：一条坏分类不应该让整批更新作废。

**抽取失败不推进水位线**，同一段对话连续失败 ``max_failures`` 次后才主动放弃并记 error——
否则一段永远解析不了的对话会让 updater 每次都被同一段内容卡住。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from evaharness.memory.capture import CapturedTurn
from evaharness.memory.schema import ProfileSection
from evaharness.memory.store import DuplicateFact, MemoryStore, UnknownFact

logger = logging.getLogger(__name__)


class ProfilePatch(BaseModel):
    """画像的一块更新。``section`` 取六块之一。

    **用枚举而不是裸 ``str``**：实测（2026-10-23）极速档下模型会把**分组名**（``user`` /
    ``history``）当成块名写进来，落库时被当作「未知的块」跳过，画像于是永远不更新。
    枚举让 JSON schema 里直接带上合法取值，也让这种输出在**解析期**就失败、触发重试，
    而不是静默丢掉一块画像。
    """

    model_config = ConfigDict(extra="ignore")

    section: ProfileSection
    summary: str


class FactAdd(BaseModel):
    """一条新事实。"""

    model_config = ConfigDict(extra="ignore")

    content: str
    category: str = "context"
    confidence: float = 0.7


class FactUpdate(BaseModel):
    """对已有事实的局部修改（只给要改的字段）。"""

    model_config = ConfigDict(extra="ignore")

    id: str
    content: str | None = None
    category: str | None = None
    confidence: float | None = None


class MemoryUpdate(BaseModel):
    """一次抽取的完整结果。**所有字段都可空**——「这段对话没有值得记住的」是合法结果。"""

    model_config = ConfigDict(extra="ignore")

    profile: list[ProfilePatch] = Field(default_factory=list)
    facts_add: list[FactAdd] = Field(default_factory=list)
    facts_update: list[FactUpdate] = Field(default_factory=list)
    facts_remove: list[str] = Field(default_factory=list)


@dataclass
class ApplyReport:
    """落库结果，日志与测试用。"""

    profile: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)


def build_extraction_messages(
    store: MemoryStore,
    turns: Sequence[CapturedTurn],
    *,
    max_facts: int = 100,
    today: str | None = None,
) -> list[BaseMessage]:
    """组装抽取请求：规则 + 现有画像/事实 + 新对话 + 输出 schema。

    **现有事实带 id 给模型**，它才能说出「取代哪一条」。事实数量按 ``max_facts`` 封顶
    （置信度高的在前），避免记忆变大后每次抽取都塞满上下文。
    """
    profile = {block.section: block.summary for block in store.profile()}
    # 置信度高的先给（稳定排序，同分时保持 `facts()` 的新在前），封顶后再转成 prompt 载荷。
    ranked = sorted(store.facts(), key=lambda fact: -fact.confidence)[: max(1, max_facts)]
    facts = [
        {
            "id": fact.id,
            "content": fact.content,
            "category": fact.category,
            "confidence": fact.confidence,
        }
        for fact in ranked
    ]
    conversation = [
        {"user": turn.user_text, "assistant": turn.assistant_text}
        for turn in turns
    ]
    schema = json.dumps(MemoryUpdate.model_json_schema(), ensure_ascii=False)
    day = today or datetime.now(UTC).date().isoformat()

    system = (
        "你是 EvaAI 的长期记忆整理器。你的唯一任务是从「新对话」里提取值得跨会话记住的信息，"
        "并与「已有记忆」合并。你只输出 JSON，不输出任何解释。\n\n"
        "规则：\n"
        "- 只记会长期成立的事：用户身份、偏好、对助手行为的纠正、长期目标、约束、重要决定、"
        "稳定的行为习惯。\n"
        "- 不记一次性的任务细节、文件路径、临时中间结果、助手自己的推测。\n"
        "- 用户纠正过的事用 category=correction，并通过 facts_update / facts_remove 处理旧记录，"
        "不要留下两条互相矛盾的记忆。\n"
        "- 与已有事实矛盾时优先替换或删除旧的，而不是再加一条。\n"
        "- profile 是六块叙述性摘要，section 只能取下面六个英文值之一，"
        "**不要写 user / history 这类分组名**：work_context（工作与专业领域）、"
        "personal_context（个人生活与喜好）、top_of_mind（当前关注的事）、"
        "recent_months（最近几个月的经历）、earlier_context（更早的上下文）、"
        "long_term_background（长期背景）。只输出有新信息的块，summary 用一段简洁的叙述，"
        "语言与用户一致。\n"
        "- facts_update / facts_remove 只能使用「已有事实」里出现过的 id；不要编造 id。\n"
        "- 没有任何值得记住的内容时，返回全空的 JSON。\n"
        f"- 今天是 {day}。\n\n"
        f"已有画像 JSON：\n{json.dumps(profile, ensure_ascii=False)}\n\n"
        f"已有事实 JSON：\n{json.dumps(facts, ensure_ascii=False)}\n\n"
        f"输出必须符合这个 JSON schema：\n{schema}"
    )
    user = f"新对话 JSON：\n{json.dumps(conversation, ensure_ascii=False)}"
    return [SystemMessage(content=system), HumanMessage(content=user)]


def tolerant_parse_update(text: str) -> MemoryUpdate:
    """从模型输出里尽量抠出 JSON（``json_object`` 模式只保证「是 JSON」，不保证干净）。

    依次尝试：整段解析 → 去掉 ```json 围栏 → 取第一个 ``{`` 到最后一个 ``}``。
    仍失败抛 ``ValueError``——调用方据此重试或放弃。
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("抽取模型返回了空内容")
    candidates = [raw]
    stripped = raw
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        candidates.append(stripped.strip())
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw[start : end + 1])
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            return MemoryUpdate.model_validate_json(candidate)
        except Exception as exc:  # pydantic 的 ValidationError / json 的 JSONDecodeError
            last_error = exc
    raise ValueError(f"抽取结果不是合法 JSON：{last_error}")


def apply_update(store: MemoryStore, update: MemoryUpdate, *, thread_id: str) -> ApplyReport:
    """把一次抽取结果落库；坏条目跳过并记日志，不让整批作废。"""
    report = ApplyReport()

    for fact_id in update.facts_remove:
        if store.forget_fact(fact_id):
            report.removed += 1
        else:
            report.skipped += 1
            report.notes.append(f"remove: 没有这条 id {fact_id!r}")

    for item in update.facts_update:
        try:
            store.update_fact(
                item.id,
                content=item.content,
                category=item.category,
                confidence=item.confidence,
            )
            report.updated += 1
        except (UnknownFact, DuplicateFact, ValueError) as exc:
            report.skipped += 1
            report.notes.append(f"update: {exc}")

    for item in update.facts_add:
        try:
            store.add_fact(
                content=item.content,
                category=item.category,
                confidence=item.confidence,
                source_thread=thread_id,
            )
            report.added += 1
        except (DuplicateFact, ValueError) as exc:
            report.skipped += 1
            report.notes.append(f"add: {exc}")

    for patch in update.profile:
        # ``section`` 是枚举，非法值在解析期就被挡下（见 :class:`ProfilePatch`），这里不再判。
        try:
            store.set_profile(patch.section.value, patch.summary)
            report.profile += 1
        except ValueError as exc:
            report.skipped += 1
            report.notes.append(f"profile: {exc}")

    if report.skipped:
        logger.warning("记忆抽取有 %d 条被跳过：%s", report.skipped, "; ".join(report.notes))
    return report


Extractor = Callable[[Sequence[BaseMessage]], Awaitable[MemoryUpdate]]
"""抽取接缝：吃一段 prompt，返回结构化更新。app 层把「建模型 + json_mode + 重试」绑进闭包。"""


class MemoryUpdater:
    """把「一段对话」变成「一次落库 + 水位线推进」。"""

    def __init__(
        self,
        store: MemoryStore,
        extract: Extractor,
        *,
        timeout: float = 60.0,
        max_facts: int = 100,
        max_failures: int = 3,
    ) -> None:
        self._store = store
        self._extract = extract
        self._timeout = timeout
        self._max_facts = max_facts
        self._max_failures = max(1, max_failures)
        self._failures: dict[str, int] = {}

    async def update(
        self,
        thread_id: str,
        turns: Sequence[CapturedTurn],
        last_message_id: str | None,
        *,
        is_stale: Callable[[], bool] = lambda: False,
    ) -> None:
        """抽取并落库。

        ``is_stale`` 在模型返回后、落库前再问一次：REST「清空全部」会递增队列 epoch，
        正在飞的这次抽取不该把旧事实写回去。
        """
        watermark = self._store.watermark(thread_id)
        if last_message_id is not None and last_message_id == watermark:
            return

        fresh = _after_watermark(turns, watermark)
        if not fresh:
            # 没有可抽取的内容（寒暄/被过滤）也要推进水位线，否则每次都会重扫同一段。
            if last_message_id is not None:
                self._store.set_watermark(thread_id, last_message_id)
            return

        try:
            messages = build_extraction_messages(self._store, fresh, max_facts=self._max_facts)
            async with asyncio.timeout(self._timeout):
                update = await self._extract(messages)
        except Exception:
            self._note_failure(thread_id, last_message_id)
            return

        if is_stale():
            logger.info("记忆抽取结果已过期（期间记忆被清空），丢弃：thread=%s", thread_id)
            return

        report = apply_update(self._store, update, thread_id=thread_id)
        if last_message_id is not None:
            self._store.set_watermark(thread_id, last_message_id)
        self._failures.pop(_failure_key(thread_id, last_message_id), None)
        logger.info(
            "记忆抽取完成：thread=%s profile=%d add=%d update=%d remove=%d skip=%d",
            thread_id,
            report.profile,
            report.added,
            report.updated,
            report.removed,
            report.skipped,
        )

    def _note_failure(self, thread_id: str, last_message_id: str | None) -> None:
        """记一次失败；连续失败到上限才推进水位线，避免永久卡在同一段对话上。"""
        key = _failure_key(thread_id, last_message_id)
        count = self._failures.get(key, 0) + 1
        self._failures[key] = count
        if count < self._max_failures:
            logger.warning("记忆抽取失败（第 %d 次），下次再试：thread=%s", count, thread_id, exc_info=True)
            return
        self._failures.pop(key, None)
        if last_message_id is not None:
            self._store.set_watermark(thread_id, last_message_id)
        logger.error("记忆抽取连续失败 %d 次，放弃这段对话：thread=%s", count, thread_id, exc_info=True)


def _failure_key(thread_id: str, last_message_id: str | None) -> str:
    return f"{thread_id}:{last_message_id or ''}"


def _after_watermark(turns: Sequence[CapturedTurn], watermark: str | None) -> list[CapturedTurn]:
    """丢掉水位线及之前的轮次（并发/重放时 capture 的窗口可能比 watermark 旧）。"""
    if watermark is None:
        return list(turns)
    for index, turn in enumerate(turns):
        if turn.last_message_id == watermark:
            return list(turns[index + 1 :])
    return list(turns)


__all__ = [
    "ApplyReport",
    "Extractor",
    "FactAdd",
    "FactUpdate",
    "MemoryUpdate",
    "MemoryUpdater",
    "ProfilePatch",
    "apply_update",
    "build_extraction_messages",
    "tolerant_parse_update",
]
