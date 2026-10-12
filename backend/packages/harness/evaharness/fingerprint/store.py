"""指纹的存储：把「上次看到的指纹」留在某处，下次启动才有得比。

``FingerprintStore`` 是个 :class:`typing.Protocol`——调用方只认这三个动作（读、写、关），
具体落在 SQLite 还是内存里是**部署策略**。两个实现：

- :class:`SqliteFingerprintStore`：生产用。与 checkpoint 体系同处 ``data/``，一个扩展名，
  单机、进程内、没有额外服务。
- :class:`InMemoryFingerprintStore`：测试用。进程一关就忘光——「第一次启动」那条路径因此
  在测试里是默认态，不用先清一个真文件。

**存储是同步的**（stdlib ``sqlite3``）：指纹的动作很少（启动期读一次、重建后写一次），
数据量也小，为它引一层异步 I/O 只会让调用点多出 ``await`` 而没有收益。真正花时间的是重建
索引（嵌入调用），那部分在别的层是异步的。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Protocol, runtime_checkable

from evaharness.fingerprint.tree import TreeFingerprint

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fingerprints (
    name       TEXT PRIMARY KEY,
    mode       TEXT NOT NULL,
    digest     TEXT NOT NULL,
    paths      TEXT NOT NULL,
    entries    TEXT NOT NULL,
    updated_at REAL NOT NULL
)
"""


@runtime_checkable
class FingerprintStore(Protocol):
    """一份指纹的读写。``name`` 是这份指纹的名字（如 ``"knowledge"``）。"""

    def load(self, name: str) -> TreeFingerprint | None:
        """上次存下的指纹；从没存过返回 ``None``（这不是错误，是「第一次」）。"""
        ...

    def save(self, name: str, fp: TreeFingerprint) -> None:
        """存下这次的指纹，覆盖同名旧值。"""
        ...

    def close(self) -> None:
        """释放底层资源（内存实现是空操作）。"""
        ...


class InMemoryFingerprintStore:
    """测试用的存储：只活在进程里。"""

    def __init__(self) -> None:
        self._data: dict[str, TreeFingerprint] = {}

    def load(self, name: str) -> TreeFingerprint | None:
        return self._data.get(name)

    def save(self, name: str, fp: TreeFingerprint) -> None:
        self._data[name] = fp

    def close(self) -> None:
        self._data.clear()


class SqliteFingerprintStore:
    """落在 SQLite 文件里的指纹存储。

    表结构是扁的：``entries`` 存成一段 JSON。没有为「按单个文件查」建索引，因为本模块的
    用法永远是整份读、整份写——把它拆成一行一个文件是给一个不存在的查询写机制。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def load(self, name: str) -> TreeFingerprint | None:
        row = self._conn.execute(
            "SELECT mode, digest, paths, entries FROM fingerprints WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return None
        mode, digest, paths, entries = row
        return TreeFingerprint(
            paths=tuple(json.loads(paths)),
            mode=mode,
            digest=digest,
            entries=json.loads(entries),
        )

    def save(self, name: str, fp: TreeFingerprint) -> None:
        self._conn.execute(
            "INSERT INTO fingerprints(name, mode, digest, paths, entries, updated_at) "
            "VALUES (?, ?, ?, ?, ?, strftime('%s','now')) "
            "ON CONFLICT(name) DO UPDATE SET "
            "mode=excluded.mode, digest=excluded.digest, paths=excluded.paths, "
            "entries=excluded.entries, updated_at=excluded.updated_at",
            (
                name,
                fp.mode,
                fp.digest,
                json.dumps(list(fp.paths), ensure_ascii=False),
                json.dumps(dict(fp.entries), ensure_ascii=False),
            ),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


__all__ = ["FingerprintStore", "InMemoryFingerprintStore", "SqliteFingerprintStore"]
