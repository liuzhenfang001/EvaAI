"""指纹与变更检测：源变了没、变哪些。

机制在 harness，**不含任何业务词**——它不知道「索引」「重建」是什么意思，只把一个目录
压成 :class:`~evaharness.fingerprint.tree.TreeFingerprint`，并给出两份指纹的差异
（:class:`~evaharness.fingerprint.detector.ChangeSet`）。谁拿这份差异去做什么，是调用方的事。

    fp = fingerprint([knowledge_root], mode="mtime")
    changes = ChangeDetector(store).diff("knowledge", fp)
    if not changes.is_empty:
        ...
"""

from __future__ import annotations

from evaharness.fingerprint.detector import ChangeDetector, ChangeSet
from evaharness.fingerprint.store import (
    FingerprintStore,
    InMemoryFingerprintStore,
    SqliteFingerprintStore,
)
from evaharness.fingerprint.tree import CONTENT, MODES, MTIME, TreeFingerprint, fingerprint

__all__ = [
    "CONTENT",
    "MODES",
    "MTIME",
    "ChangeDetector",
    "ChangeSet",
    "FingerprintStore",
    "InMemoryFingerprintStore",
    "SqliteFingerprintStore",
    "TreeFingerprint",
    "fingerprint",
]
