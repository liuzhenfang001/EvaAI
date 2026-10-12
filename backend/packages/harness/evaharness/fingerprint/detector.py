"""变更检测：把「这次指纹」与「上次存下的指纹」比出差异。

它**只回答「变哪些」**，不决定任何动作——「变了就全量重建」还是「按文件增量重建」是调用方
的事。所以这里返回的是 :class:`ChangeSet`（三个路径集合），而不是一个 bool。

**为什么不是 bool**：bool 只够支撑「全量重建 or 跳过」；而一旦要做增量重建，你需要知道
**具体哪个文件**增了 / 删了 / 改了——整棵树的摘要一变，那份信息就丢了。返回集合的代价几乎
为零（多一个 dataclass），却把唯一那条更贵的选择留着。
"""

from __future__ import annotations

from dataclasses import dataclass

from evaharness.fingerprint.store import FingerprintStore
from evaharness.fingerprint.tree import TreeFingerprint


@dataclass(frozen=True, slots=True)
class ChangeSet:
    """两组指纹之间的差异。三个集合都是相对路径，且**有序**（便于测试与日志稳定）。"""

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        """这次没变——可以跳过重建。"""
        return not (self.added or self.removed or self.modified)

    @property
    def changed(self) -> tuple[str, ...]:
        """需要**重新处理**的路径：新增 + 修改。删除的另算（它是「清掉」，不是「重算」）。"""
        return tuple(sorted((*self.added, *self.modified)))


class ChangeDetector:
    """拿一份指纹与存储里的旧指纹比差异。

    存储通过构造函数注入（:class:`~evaharness.fingerprint.store.FingerprintStore`），
    所以「上次是什么」是可替换的：生产里是 SQLite，测试里是内存。
    """

    def __init__(self, store: FingerprintStore) -> None:
        self._store = store

    def diff(self, name: str, fp: TreeFingerprint) -> ChangeSet:
        """算出 ``fp`` 相对「上次存下的 ``name``」的差异。

        **第一次**（存储里没有）时，所有文件都算 ``added``——这正是「首次全量建索引」想要的
        语义，不需要在调用方再写一条 ``if prev is None`` 的分支。
        """
        previous = self._store.load(name)
        if previous is None:
            return ChangeSet(added=fp.files)

        old = previous.entries
        new = fp.entries
        added = tuple(sorted(key for key in new if key not in old))
        removed = tuple(sorted(key for key in old if key not in new))
        modified = tuple(
            sorted(key for key in new if key in old and new[key] != old[key])
        )
        return ChangeSet(added=added, removed=removed, modified=modified)

    def is_changed(self, name: str, fp: TreeFingerprint) -> bool:
        """只问「变没变」。第一次也算变（存储里没有可比的东西）。"""
        previous = self._store.load(name)
        return previous is None or previous.digest != fp.digest


__all__ = ["ChangeDetector", "ChangeSet"]
