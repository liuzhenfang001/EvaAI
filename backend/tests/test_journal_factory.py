"""契约测试：按配置选 JournalStore 实现。"""

import pytest

from evaharness.config.app_config import JournalConfig
from evaharness.journal.factory import make_journal_store
from evaharness.journal.stores import JsonlJournalStore, MemoryJournalStore
from evaharness.reflection import ReflectionError


def test_default_config_builds_a_jsonl_store(tmp_path):
    """默认落盘而非内存：不落盘的话「回放」无从谈起。"""
    store = make_journal_store(JournalConfig(dir=str(tmp_path / "journal")))

    assert isinstance(store, JsonlJournalStore)
    assert store.dir == tmp_path / "journal"


def test_store_can_be_swapped_by_class_path():
    store = make_journal_store(JournalConfig(store="evaharness.journal.stores:MemoryJournalStore"))

    assert isinstance(store, MemoryJournalStore)


def test_extra_keys_are_forwarded_to_the_store_constructor(tmp_path):
    config = JournalConfig(
        store="evaharness.journal.stores:JsonlJournalStore",
        dir=str(tmp_path / "别处"),
        flush_threshold=5,
    )

    store = make_journal_store(config)

    assert store.dir == tmp_path / "别处"


def test_journal_only_keys_are_not_forwarded(tmp_path):
    """``flush_threshold`` 是 RunJournal 的参数，不是 store 的——传下去会 TypeError。"""
    config = JournalConfig(dir=str(tmp_path), flush_threshold=7)

    make_journal_store(config)  # 不抛即通过


def test_unknown_class_path_fails_loudly():
    with pytest.raises(ReflectionError):
        make_journal_store(JournalConfig(store="evaharness.journal.stores:查无此类"))


def test_a_class_that_is_not_a_store_is_rejected():
    with pytest.raises(ReflectionError):
        make_journal_store(JournalConfig(store="evaharness.journal.stores:JournalEntry"))
