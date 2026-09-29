"""RunJournal 的可插拔 store。

两个实现（内存 / JSONL）足以证明「可插拔」不是空话，也覆盖了两种真实用途：
测试与临时运行用内存，本地持久化用 JSONL（可以直接 tail / grep 看 trace）。

**为什么用 ``abc.ABC`` 而不是 ``Protocol``**：选实现走
:func:`evaharness.reflection.resolve_class` 的 ``issubclass`` 校验，
它对 ABC 是清晰的；``Protocol`` 需要 ``@runtime_checkable`` 且只能查方法名。

**没有 per-thread 写锁**：参照项目 DeerFlow 的 store 有按 thread 的锁，因为它的
事件来自多路并发 callback；EvaAI 的 journal 由编排器的单条事件流喂入，写入天然
串行（见 ADR-0008）。这里不为不存在的问题加锁。
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from evaharness.agents.plan_exec.events import RunEvent

logger = logging.getLogger(__name__)

# run_id 会成为文件名，而它来自 URL，所以必须挡住路径穿越。
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class JournalEntry(BaseModel):
    """RunJournal 里的一条记录：事件 + 它在本次运行中的全序编号。"""

    seq: int
    event: RunEvent


class JournalRun(BaseModel):
    """一次运行的概要，供「最近运行」列表使用。"""

    run_id: str
    updated_at: str


class JournalStore(abc.ABC):
    """RunJournal 的存储后端契约。"""

    @abc.abstractmethod
    async def append(self, run_id: str, entries: Sequence[JournalEntry]) -> None:
        """把一批记录追加到 *run_id* 名下。"""

    @abc.abstractmethod
    async def load(self, run_id: str) -> list[JournalEntry]:
        """按 seq 升序取出 *run_id* 的全部记录；未知 run 返回空列表。"""

    @abc.abstractmethod
    async def runs(self) -> list[JournalRun]:
        """列出已有运行，最近的在前。"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class MemoryJournalStore(JournalStore):
    """进程内存储：测试与不关心持久化的场景用。"""

    def __init__(self) -> None:
        self._entries: dict[str, list[JournalEntry]] = {}
        self._updated: dict[str, str] = {}
        # 时间戳可能撞在同一微秒上，光靠它排序不稳定——用一个自增序列兜底。
        self._stamp: dict[str, int] = {}
        self._counter = 0

    async def append(self, run_id: str, entries: Sequence[JournalEntry]) -> None:
        if not entries:
            return
        self._entries.setdefault(run_id, []).extend(entries)
        self._counter += 1
        self._stamp[run_id] = self._counter
        self._updated[run_id] = _now()

    async def load(self, run_id: str) -> list[JournalEntry]:
        return list(self._entries.get(run_id, ()))

    async def runs(self) -> list[JournalRun]:
        ordered = sorted(self._entries, key=lambda run_id: self._stamp[run_id], reverse=True)
        return [JournalRun(run_id=run_id, updated_at=self._updated[run_id]) for run_id in ordered]


class JsonlJournalStore(JournalStore):
    """一行一条记录的 JSONL 文件，每个 run 一个文件：``<dir>/<run_id>.jsonl``。

    选它的理由是**可读**：排查时直接 ``tail`` 就能看到这次运行发生了什么，不需要
    连数据库。代价是没有索引，所以 :meth:`runs` 靠文件 mtime 排序。
    """

    def __init__(self, dir: str | Path = "./data/journal") -> None:
        self.dir = Path(dir)

    def _path(self, run_id: str) -> Path:
        if not _SAFE_RUN_ID.match(run_id):
            raise ValueError(f"非法的 run_id：{run_id!r}")
        return self.dir / f"{run_id}.jsonl"

    async def append(self, run_id: str, entries: Sequence[JournalEntry]) -> None:
        if not entries:
            return
        path = self._path(run_id)
        lines = "".join(json.dumps(entry.model_dump(mode="json"), ensure_ascii=False) + "\n" for entry in entries)
        await asyncio.to_thread(_append_text, path, lines)

    async def load(self, run_id: str) -> list[JournalEntry]:
        path = self._path(run_id)
        if not path.is_file():
            return []
        text = await asyncio.to_thread(path.read_text, "utf-8")
        entries: list[JournalEntry] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                entries.append(JournalEntry.model_validate(json.loads(line)))
            except (json.JSONDecodeError, ValidationError):
                # 硬杀进程会留下半截行；跳过它，而不是让整次回放炸掉。
                logger.warning("跳过无法解析的 journal 行：%s", path)
        return entries

    async def runs(self) -> list[JournalRun]:
        if not self.dir.is_dir():
            return []
        paths = await asyncio.to_thread(_list_jsonl, self.dir)
        return [JournalRun(run_id=path.stem, updated_at=_mtime_iso(path)) for path in paths]


def _append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def _list_jsonl(directory: Path) -> list[Path]:
    # mtime 精度可能不足以区分连续两次运行，用文件名做稳定的次级排序键。
    return sorted(directory.glob("*.jsonl"), key=lambda path: (path.stat().st_mtime, path.stem), reverse=True)


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()


__all__ = [
    "JournalEntry",
    "JournalRun",
    "JournalStore",
    "JsonlJournalStore",
    "MemoryJournalStore",
]
