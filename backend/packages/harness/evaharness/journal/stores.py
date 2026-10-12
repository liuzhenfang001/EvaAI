"""RunJournal 的可插拔 store：按**会话（thread）**组织，一个会话一个文件。

两个实现（内存 / JSONL）足以证明「可插拔」不是空话，也覆盖了两种真实用途：
测试与临时运行用内存，本地持久化用 JSONL（可以直接 tail / grep 看 trace）。

**为什么是一个会话一个文件**（`<dir>/<thread_id>/journal.jsonl`）：早先按「一次运行一个
文件」存（T3 的布局，参照项目也是那样），但 T7 之后**我们从来不单独读某一次运行**——
`/threads/{tid}/journal` 永远整段读。于是按 run 分文件一点好处都没换来，反而把跨运行的
顺序搞难了：文件内顺理成章的行序没了，得逐个打开文件读 `run_start.started_at` 才能排序。
合成一个文件后，**行序天然就是跨运行的全序**，而且标题、摘要、运行次数、最近一次运行的
id 全都从**同一次读取**里得到（见 :meth:`JsonlJournalStore._thread_summary`）。

代价是**同一会话的两次运行并发写会交错**。UI 在流式期间禁用输入，所以当前不会发生；
真要支持并发，要么按 run 再拆文件，要么像参照项目那样用「一个会话同时只允许一个活跃
run」的约束挡住（它用 DB 的部分唯一索引实现）。见 ADR-0009。

**store 为什么同时管「会话元数据」**：重命名需要一处**可变**存储，而事件流是纯追加的，
给不了。参照项目为此单开了一张 ``threads_meta`` 表；我们只有一个标题要存，为它再加一层
接口与配置不值当。代价是每个实现都要多实现几个方法——两个实现下可以接受，但**这是本
文件最可能被后人质疑的一处**：会话元数据长起来（置顶、标签、成员）时应当拆出去。

**没有写锁**：journal 由编排器的单条事件流喂入，写入天然串行（ADR-0008）。
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import re
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from evaharness.runtime import MessagesEvent, RunEvent, RunStartEvent

logger = logging.getLogger(__name__)

# thread_id 会成为路径段，而它来自 URL —— 必须挡住路径穿越。
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_TITLE_MAX_LENGTH = 40
_PREVIEW_MAX_LENGTH = 60
_JOURNAL_FILENAME = "journal.jsonl"
_META_FILENAME = "meta.json"


class JournalEntry(BaseModel):
    """RunJournal 里的一条记录：事件 + 它在本次会话中的顺序编号。

    ``seq`` 严格递增但**可能跳号**——写盘归并会把同组的后续编号吃掉（见 compaction.py）。
    """

    seq: int
    event: RunEvent


class JournalThread(BaseModel):
    """一个会话的概要（供左侧会话列表）。"""

    thread_id: str
    title: str
    updated_at: str
    run_count: int
    last_run_id: str | None = None
    preview: str = ""
    """最近一次运行里最后一段助手正文——列表上「这个会话聊到哪了」的一行摘要。

    它取的是 ``category == "message"`` 那个桶，是「消费类路由桶」这一设计
    （ADR-0009）在服务端的实际用武之地。
    """


class JournalStore(abc.ABC):
    """RunJournal 的存储后端契约。"""

    @abc.abstractmethod
    async def append(self, thread_id: str, entries: Sequence[JournalEntry]) -> None:
        """把一批记录追加到会话 *thread_id* 的记录末尾。"""

    @abc.abstractmethod
    async def load(self, thread_id: str) -> list[JournalEntry]:
        """按行序取出会话的全部记录；未知会话返回空列表。"""

    @abc.abstractmethod
    async def threads(self) -> list[JournalThread]:
        """列出全部会话，最近的在前。"""

    @abc.abstractmethod
    async def rename_thread(self, thread_id: str, title: str) -> None:
        """给会话起一个自定义标题（覆盖从首个请求推导出来的那个）。"""

    @abc.abstractmethod
    async def delete_thread(self, thread_id: str) -> None:
        """**真删**该会话的全部记录（ADR-0009：不做回收站）。"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def validate_id(value: str, *, kind: str) -> str:
    """校验 thread_id；不合法就抛 ``ValueError``（路由层转 404）。"""
    if not _SAFE_ID.match(value):
        raise ValueError(f"非法的 {kind}：{value!r}")
    return value


def summarize_as_title(event: RunEvent) -> str | None:
    """从一次运行的首个事件里取会话标题（取用户原始请求的前若干字）。"""
    if not isinstance(event, RunStartEvent):
        return None
    text = " ".join(event.request.split())
    if not text:
        return None
    return text if len(text) <= _TITLE_MAX_LENGTH else text[: _TITLE_MAX_LENGTH - 1] + "…"


def summarize_as_preview(entries: Sequence[JournalEntry]) -> str:
    """最近一段助手正文，用作会话列表的一行摘要。

    只看 ``category == "message"`` 那个桶，且只取**最后一条有正文的** ``messages``
    事件——落盘时同一条消息已被归并，所以这一条就是整段正文。
    """
    preview = ""
    for entry in entries:
        event = entry.event
        if event.category != "message" or not isinstance(event, MessagesEvent):
            continue
        chunk = event.chunk
        if chunk.get("type") == "tool" or chunk.get("tool_calls") or chunk.get("tool_call_chunks"):
            continue
        content = chunk.get("content")
        if isinstance(content, str) and content.strip():
            preview = " ".join(content.split())
    if len(preview) > _PREVIEW_MAX_LENGTH:
        preview = preview[: _PREVIEW_MAX_LENGTH - 1] + "…"
    return preview


def _run_starts(entries: Sequence[JournalEntry]) -> list[RunStartEvent]:
    return [entry.event for entry in entries if isinstance(entry.event, RunStartEvent)]


def _read_entries(path: Path) -> list[JournalEntry]:
    if not path.is_file():
        return []
    entries: list[JournalEntry] = []
    for line in path.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entries.append(JournalEntry.model_validate(json.loads(line)))
        except (json.JSONDecodeError, ValidationError):
            # 硬杀进程会留下半截行；跳过它，而不是让整段会话炸掉。
            logger.warning("跳过无法解析的 journal 行：%s", path)
    return entries


class MemoryJournalStore(JournalStore):
    """进程内存储：测试与不关心持久化的场景用。"""

    def __init__(self) -> None:
        self._entries: dict[str, list[JournalEntry]] = {}
        self._titles: dict[str, str] = {}
        # 同一微秒内连续写入会让时间戳撞车，光靠它排序不稳定——用自增序列兜底。
        self._stamp: dict[str, int] = {}
        self._updated: dict[str, str] = {}
        self._counter = 0

    async def append(self, thread_id: str, entries: Sequence[JournalEntry]) -> None:
        if not entries:
            return
        self._entries.setdefault(thread_id, []).extend(entries)
        self._counter += 1
        self._stamp[thread_id] = self._counter
        self._updated[thread_id] = _now()

    async def load(self, thread_id: str) -> list[JournalEntry]:
        return list(self._entries.get(thread_id, ()))

    async def threads(self) -> list[JournalThread]:
        ordered = sorted(self._entries, key=lambda thread_id: self._stamp.get(thread_id, 0), reverse=True)
        threads = [self._summary(thread_id) for thread_id in ordered]
        return [thread for thread in threads if thread is not None]

    async def rename_thread(self, thread_id: str, title: str) -> None:
        self._titles[thread_id] = title

    async def delete_thread(self, thread_id: str) -> None:
        self._entries.pop(thread_id, None)
        self._titles.pop(thread_id, None)
        self._stamp.pop(thread_id, None)
        self._updated.pop(thread_id, None)

    def _summary(self, thread_id: str) -> JournalThread | None:
        entries = self._entries.get(thread_id, [])
        if not entries:
            return None
        starts = _run_starts(entries)
        return JournalThread(
            thread_id=thread_id,
            title=self._titles.get(thread_id) or (summarize_as_title(starts[0]) if starts else None) or thread_id,
            updated_at=self._updated.get(thread_id, ""),
            run_count=len(starts),
            last_run_id=starts[-1].run_id if starts else None,
            preview=summarize_as_preview(entries),
        )


class JsonlJournalStore(JournalStore):
    """一个会话一个目录：``<dir>/<thread_id>/journal.jsonl``（外加 ``meta.json``）。

    选 JSONL 的理由是**可读**：排查时直接 ``tail`` 就能看到这段会话发生了什么，不需要
    连数据库。行序就是跨运行的全序，所以取整段会话只需要一次文件读。
    """

    def __init__(self, dir: str | Path = "./data/journal") -> None:
        self.dir = Path(dir)

    # ---- 路径 ------------------------------------------------------------- #

    def _thread_dir(self, thread_id: str) -> Path:
        return self.dir / validate_id(thread_id, kind="thread_id")

    def _journal_path(self, thread_id: str) -> Path:
        return self._thread_dir(thread_id) / _JOURNAL_FILENAME

    def _meta_path(self, thread_id: str) -> Path:
        return self._thread_dir(thread_id) / _META_FILENAME

    # ---- 契约 ------------------------------------------------------------- #

    async def append(self, thread_id: str, entries: Sequence[JournalEntry]) -> None:
        if not entries:
            return
        path = self._journal_path(thread_id)
        lines = "".join(json.dumps(entry.model_dump(mode="json"), ensure_ascii=False) + "\n" for entry in entries)
        await asyncio.to_thread(_append_text, path, lines)

    async def load(self, thread_id: str) -> list[JournalEntry]:
        return await asyncio.to_thread(_read_entries, self._journal_path(thread_id))

    async def threads(self) -> list[JournalThread]:
        if not self.dir.is_dir():
            return []
        directories = await asyncio.to_thread(_list_thread_dirs, self.dir)
        threads = [await self._thread_summary(directory) for directory in directories]
        present = [thread for thread in threads if thread is not None]
        return sorted(present, key=lambda thread: (thread.updated_at, thread.thread_id), reverse=True)

    async def rename_thread(self, thread_id: str, title: str) -> None:
        path = self._meta_path(thread_id)
        payload = json.dumps({"title": title}, ensure_ascii=False)
        await asyncio.to_thread(_write_text, path, payload)

    async def delete_thread(self, thread_id: str) -> None:
        directory = self._thread_dir(thread_id)
        if directory.is_dir():
            await asyncio.to_thread(shutil.rmtree, directory, True)

    # ---- 内部 ------------------------------------------------------------- #

    async def _thread_summary(self, directory: Path) -> JournalThread | None:
        """标题、摘要、运行次数、最近一次运行的 id —— 全从**同一次读取**里得到。

        判据只有一条「有没有 journal.jsonl」：只要文件在就列出来。曾经想「没有
        ``run_start`` 就不算会话」，但那会让数据**存在却查不到**（``load`` 读得到、
        ``threads`` 不列出），前后不一致。
        """
        thread_id = directory.name
        if not _SAFE_ID.match(thread_id):
            return None
        journal = directory / _JOURNAL_FILENAME
        if not journal.is_file():
            return None
        entries = await asyncio.to_thread(_read_entries, journal)
        starts = _run_starts(entries)
        return JournalThread(
            thread_id=thread_id,
            title=await asyncio.to_thread(self._title_of, directory, starts[0] if starts else None),
            updated_at=await asyncio.to_thread(_mtime_iso, journal),
            run_count=len(starts),
            last_run_id=starts[-1].run_id if starts else None,
            preview=summarize_as_preview(entries),
        )

    def _title_of(self, directory: Path, first_start: RunStartEvent | None) -> str:
        """自定义标题优先；否则用会话**第一次运行**的用户请求推导。"""
        meta = directory / _META_FILENAME
        if meta.is_file():
            try:
                title = json.loads(meta.read_text("utf-8")).get("title")
            except (json.JSONDecodeError, AttributeError):
                title = None
            if title:
                return str(title)
        if first_start is not None:
            derived = summarize_as_title(first_start)
            if derived:
                return derived
        return directory.name


def _append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _list_thread_dirs(root: Path) -> list[Path]:
    return [path for path in root.iterdir() if path.is_dir() and _SAFE_ID.match(path.name)]


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()


__all__ = [
    "JournalEntry",
    "JournalStore",
    "JournalThread",
    "JsonlJournalStore",
    "MemoryJournalStore",
    "summarize_as_preview",
    "summarize_as_title",
    "validate_id",
]
