"""按配置选一个 JournalStore 实现。

与 model factory 同一套约定：``store`` 是 ``"module.path:ClassName"`` 类路径，
其余键透传给它的构造函数。选实现走 :func:`evaharness.reflection.resolve_class`，
所以写错一个类名会在**启动时**就炸，而不是等到第一次记账才悄悄失败。
"""

from __future__ import annotations

from evaharness.config.app_config import JournalConfig
from evaharness.journal.stores import JournalStore
from evaharness.reflection import resolve_class

# 属于 RunJournal 而非 store 构造函数的键，不能透传下去。
_JOURNAL_ONLY_FIELDS = frozenset({"store", "flush_threshold"})


def make_journal_store(config: JournalConfig) -> JournalStore:
    """按 *config* 建出 JournalStore。

    Args:
        config: ``config.yaml`` 的 ``journal`` 段。

    Returns:
        ``config.store`` 指向的 store 实例。

    Raises:
        ReflectionError: 类路径无法解析，或解析出来的不是 :class:`JournalStore`。
    """
    store_class = resolve_class(config.store, JournalStore)
    settings = config.model_dump(exclude_none=True, exclude=_JOURNAL_ONLY_FIELDS)
    return store_class(**settings)


__all__ = ["make_journal_store"]
