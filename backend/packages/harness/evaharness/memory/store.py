"""记忆的存储：画像一块一行、事实一条一行、水位线一会话一行。

**为什么是 SQLite**：与 checkpoint、指纹同处 ``data/``，一个文件、单机、进程内、没有额外服务；
增删改查与管理页要的「按 id 删一条」都是它原生擅长的。参照项目把每条事实存成一个 Markdown
文件（sha256 分片 + manifest + 乐观锁）——那是为**多用户与跨进程**写的，我们单用户单进程，
抄的是它的**字段形状**，不是它的分片机制。

**存储是同步的**（stdlib ``sqlite3``，与 :mod:`evaharness.fingerprint.store` 同一口径）：这里的
动作都是小查询，为它引一层异步 I/O 只会让每个调用点多一个 ``await``。代价照实记：注入中间件
每轮要在事件循环里同步读一次，量级是「一张小表上的一条索引查询」。

**连接有线程亲和性**：``sqlite3`` 默认只许在**建连接的那个线程**上用。所以这个对象在 lifespan
（事件循环线程）里建，之后的读取也都发生在那儿——三件工具因此写成 ``async def``（同步工具会被
langgraph 丢进工作线程），别把它们改成同步函数，也别把库的调用丢进 ``asyncio.to_thread``。

**``memory_facts.content`` 的唯一索引是去重的唯一防线**。撞了要抛 :class:`DuplicateFact`，
不能静默吞——「记了两遍」与「记了一遍」在用户眼里是两件事，在注入里就是两行废话。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from evaharness.memory.schema import (
    DEFAULT_FACT_CATEGORY,
    DEFAULT_FACT_CONFIDENCE,
    FACT_CATEGORIES,
    PROFILE_GROUPS,
    Fact,
    MemorySnapshot,
    ProfileBlock,
    new_fact_id,
)

_PROFILE_SECTIONS: tuple[str, ...] = tuple(
    section.value for group in PROFILE_GROUPS.values() for section in group
)
"""六块的**规范顺序**（``user`` 组在前、``history`` 组在后），渲染与读取都按它排。"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_profile (
    section    TEXT PRIMARY KEY,
    summary    TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_facts (
    seq           INTEGER PRIMARY KEY,
    id            TEXT NOT NULL UNIQUE,
    content       TEXT NOT NULL,
    category      TEXT NOT NULL,
    confidence    REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',
    source_thread TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS memory_facts_content ON memory_facts(content);

CREATE TABLE IF NOT EXISTS memory_watermarks (
    thread_id       TEXT PRIMARY KEY,
    last_message_id TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""


class MemoryError(Exception):
    """记忆存储的错误基类。"""


class DuplicateFact(MemoryError):
    """同一条内容已经记过了（``content`` 唯一索引）。"""


class UnknownFact(MemoryError):
    """按 id 找不到这条事实。"""


def _now() -> str:
    """ISO 8601 UTC、精确到秒。前端 ``new Date(...)`` 直接可解析——参照项目在时间戳上踩过坑。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalize_since(value: str) -> str:
    """把用户/模型给的起始时间归一化成与 :func:`_now` 同一形状。

    **必须归一化**：``updated_at`` 是按 ISO 字符串比较的（``YYYY-MM-DDTHH:MM:SS+00:00``），
    给它一个 ``2026-10-01``、``2026-10-01T00:00:00Z`` 或带其它偏移量的值，字典序比较就会
    给出错误的上下界。这里统一成「UTC、精确到秒、``+00:00`` 后缀」。

    不带时区的时间**按 UTC 解释**（部署是本地单机，所有写入本来就是 UTC）；空串与解析不了
    的值抛 ``ValueError``——调用方（工具）据此给模型一句可读的否定结果。
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("起始时间不能为空")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"看不懂这个时间：{value!r}（请用 ISO 8601，例如 2026-10-01 或 2026-10-01T12:00:00Z）"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def _validate(content: str, category: str, confidence: float) -> tuple[str, str, float]:
    """三条闸门，放在**存储这一个咽喉**上，两个调用方（工具 / 管理页）走同一条。"""
    text = (content or "").strip()
    if not text:
        raise ValueError("记忆内容不能为空")
    if category not in FACT_CATEGORIES:
        raise ValueError(f"未知的事实分类 {category!r}，可选：{', '.join(sorted(FACT_CATEGORIES))}")
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"置信度要在 0 与 1 之间，收到 {confidence!r}")
    return text, category, float(confidence)


class MemoryStore(Protocol):
    """记忆的读写面。调用方只认这些动作，落在 SQLite 还是别处是部署策略。"""

    def snapshot(self) -> MemorySnapshot:
        """一次读出画像与全部生效事实——注入路径每轮走这一个（一次事务，不做两次查询）。"""
        ...

    def profile(self) -> list[ProfileBlock]:
        """六块画像里**写过的那几块**，按 :data:`PROFILE_GROUPS` 的规范顺序。"""
        ...

    def set_profile(self, section: str, summary: str) -> ProfileBlock:
        """写一块画像（覆盖同名旧值）。"""
        ...

    def delete_profile(self, section: str) -> bool:
        """抹掉一块画像。抹掉了返回 ``True``，本来就没有返回 ``False``。"""
        ...

    def facts(self, *, category: str | None = None, include_superseded: bool = False) -> list[Fact]:
        """事实列表，新的在前。"""
        ...

    def fact(self, fact_id: str) -> Fact | None:
        """按 id 取一条；没有返回 ``None``（「没有」不是错误）。"""
        ...

    def add_fact(
        self,
        *,
        content: str,
        category: str = DEFAULT_FACT_CATEGORY,
        confidence: float = DEFAULT_FACT_CONFIDENCE,
        source_thread: str | None = None,
    ) -> Fact:
        """记一条。内容重复抛 :class:`DuplicateFact`。"""
        ...

    def update_fact(
        self,
        fact_id: str,
        *,
        content: str | None = None,
        category: str | None = None,
        confidence: float | None = None,
    ) -> Fact:
        """改一条的局部字段。id 不存在抛 :class:`UnknownFact`，改完撞内容抛 :class:`DuplicateFact`。"""
        ...

    def forget_fact(self, fact_id: str) -> bool:
        """删一条。删掉了返回 ``True``，本来就没有返回 ``False``（都算成功）。"""
        ...

    def search_facts(
        self,
        query: str,
        *,
        category: str | None = None,
        since: str | None = None,
        limit: int = 10,
    ) -> list[Fact]:
        """内容子串匹配（大小写不敏感），按置信度降序。

        ``since`` 是按 ``updated_at`` 过滤的**起始时间**（ISO 8601）。``query`` 与 ``since``
        **都为空**才返回空表；只给 ``since`` 是合法的——那是在问「最近更新了什么」。
        """
        ...

    def clear(self) -> None:
        """清空事实与画像。**水位线一并清**——留着它会让人以为「这段对话已经抽过了」。"""
        ...

    def watermark(self, thread_id: str) -> str | None:
        """这个会话抽到哪条消息了（T21 用）。没抽过返回 ``None``。"""
        ...

    def set_watermark(self, thread_id: str, last_message_id: str) -> None:
        """推进水位线（T21 用）。"""
        ...

    def close(self) -> None:
        """释放底层资源。"""
        ...


class SqliteMemoryStore:
    """落在 SQLite 文件里的记忆存储。"""

    def __init__(self, path: str | Path, *, now: Callable[[], str] = _now) -> None:
        """``now`` 是**可注入的时钟**：测试靠它把「同一秒内的两次更新」区分开，
        否则 ``since`` 过滤在同秒写入下根本没法断言（``created_at`` 只到秒）。"""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ---- 画像 ----------------------------------------------------------------

    def profile(self) -> list[ProfileBlock]:
        rows = {row["section"]: row for row in self._conn.execute("SELECT * FROM memory_profile")}
        # 按规范顺序排（而不是按写入顺序）：注入的字节稳定性由这里保证，库里怎么插都无所谓。
        return [
            ProfileBlock(
                section=name,
                summary=rows[name]["summary"],
                updated_at=rows[name]["updated_at"],
            )
            for name in _PROFILE_SECTIONS
            if name in rows
        ]

    def set_profile(self, section: str, summary: str) -> ProfileBlock:
        if section not in _PROFILE_SECTIONS:
            raise ValueError(f"未知的画像块 {section!r}")
        text = (summary or "").strip()
        if not text:
            raise ValueError("画像摘要不能为空——要清掉一块请用 delete_profile")
        stamp = self._now()
        self._conn.execute(
            "INSERT INTO memory_profile(section, summary, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(section) DO UPDATE SET summary=excluded.summary, updated_at=excluded.updated_at",
            (section, text, stamp),
        )
        self._conn.commit()
        return ProfileBlock(section=section, summary=text, updated_at=stamp)

    def delete_profile(self, section: str) -> bool:
        """抹掉一块画像。空摘要没有意义，所以「清掉」是一个显式动作，不是写空串。"""
        cursor = self._conn.execute("DELETE FROM memory_profile WHERE section = ?", (section,))
        self._conn.commit()
        return cursor.rowcount > 0

    # ---- 事实 ----------------------------------------------------------------

    def facts(self, *, category: str | None = None, include_superseded: bool = False) -> list[Fact]:
        sql = "SELECT * FROM memory_facts"
        clauses: list[str] = []
        params: list[object] = []
        if not include_superseded:
            clauses.append("status = 'active'")
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        # 按自增序号倒序，**不**按时间戳：`created_at` 只到秒，同一秒记下的两条会打平，
        # 而打平后的顺序若是随机的（按 id 排 = 按 uuid 排），「新的在前」就成了假的。
        sql += " ORDER BY seq DESC"
        return [_row_to_fact(row) for row in self._conn.execute(sql, params)]

    def fact(self, fact_id: str) -> Fact | None:
        row = self._conn.execute("SELECT * FROM memory_facts WHERE id = ?", (fact_id,)).fetchone()
        return _row_to_fact(row) if row is not None else None

    def add_fact(
        self,
        *,
        content: str,
        category: str = DEFAULT_FACT_CATEGORY,
        confidence: float = DEFAULT_FACT_CONFIDENCE,
        source_thread: str | None = None,
    ) -> Fact:
        text, category, confidence = _validate(content, category, confidence)
        fact_id = new_fact_id()
        stamp = self._now()
        try:
            self._conn.execute(
                "INSERT INTO memory_facts"
                "(id, content, category, confidence, status, source_thread, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
                (fact_id, text, category, confidence, source_thread, stamp, stamp),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateFact(f"这条已经记过了：{text}") from exc
        self._conn.commit()
        return Fact(
            content=text,
            category=category,
            confidence=confidence,
            id=fact_id,
            status="active",
            source_thread=source_thread,
            created_at=stamp,
            updated_at=stamp,
        )

    def update_fact(
        self,
        fact_id: str,
        *,
        content: str | None = None,
        category: str | None = None,
        confidence: float | None = None,
    ) -> Fact:
        current = self.fact(fact_id)
        if current is None:
            raise UnknownFact(f"没有这条记忆：{fact_id}")
        text, category, confidence = _validate(
            content if content is not None else current.content,
            category if category is not None else current.category,
            confidence if confidence is not None else current.confidence,
        )
        stamp = self._now()
        try:
            self._conn.execute(
                "UPDATE memory_facts SET content = ?, category = ?, confidence = ?, updated_at = ? WHERE id = ?",
                (text, category, confidence, stamp, fact_id),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateFact(f"改完会与另一条重复：{text}") from exc
        self._conn.commit()
        return Fact(
            content=text,
            category=category,
            confidence=confidence,
            id=fact_id,
            status=current.status,
            source_thread=current.source_thread,
            created_at=current.created_at,
            updated_at=stamp,
        )

    def forget_fact(self, fact_id: str) -> bool:
        cursor = self._conn.execute("DELETE FROM memory_facts WHERE id = ?", (fact_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    def search_facts(
        self,
        query: str,
        *,
        category: str | None = None,
        since: str | None = None,
        limit: int = 10,
    ) -> list[Fact]:
        needle = (query or "").strip()
        since_value = normalize_since(since) if since is not None and since.strip() else None
        if not needle and since_value is None:
            return []
        sql = "SELECT * FROM memory_facts WHERE status = 'active'"
        params: list[object] = []
        if needle:
            sql += " AND content LIKE ? ESCAPE '\\'"
            params.append(f"%{_escape_like(needle)}%")
        if category is not None:
            sql += " AND category = ?"
            params.append(category)
        if since_value is not None:
            sql += " AND updated_at >= ?"
            params.append(since_value)
        # 有查询词时按「相关度」（置信度）排；只给时间时按「最近更新」排——那才是用户问的。
        order = "confidence DESC, seq DESC" if needle else "updated_at DESC, seq DESC"
        sql += f" ORDER BY {order} LIMIT ?"
        params.append(max(1, limit))
        return [_row_to_fact(row) for row in self._conn.execute(sql, params)]

    # ---- 水位线（T21） --------------------------------------------------------

    def watermark(self, thread_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT last_message_id FROM memory_watermarks WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return None if row is None else str(row["last_message_id"])

    def set_watermark(self, thread_id: str, last_message_id: str) -> None:
        self._conn.execute(
            "INSERT INTO memory_watermarks(thread_id, last_message_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(thread_id) DO UPDATE SET last_message_id=excluded.last_message_id, "
            "updated_at=excluded.updated_at",
            (thread_id, last_message_id, self._now()),
        )
        self._conn.commit()

    # ---- 整体 ----------------------------------------------------------------

    def snapshot(self) -> MemorySnapshot:
        return MemorySnapshot(profile=tuple(self.profile()), facts=tuple(self.facts()))

    def clear(self) -> None:
        self._conn.execute("DELETE FROM memory_facts")
        self._conn.execute("DELETE FROM memory_profile")
        self._conn.execute("DELETE FROM memory_watermarks")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def _row_to_fact(row: sqlite3.Row) -> Fact:
    return Fact(
        content=row["content"],
        category=row["category"],
        confidence=float(row["confidence"]),
        id=row["id"],
        status=row["status"],
        source_thread=row["source_thread"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _escape_like(text: str) -> str:
    """让用户输入里的 ``%`` / ``_`` 当字面量——否则「100%」会变成通配。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


__all__ = [
    "DuplicateFact",
    "MemoryError",
    "MemoryStore",
    "SqliteMemoryStore",
    "UnknownFact",
]
