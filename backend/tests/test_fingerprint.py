"""指纹与变更检测单测：增 / 删 / 改 / 无变化四种情形，两种 mode，ignore，空目录与不存在的路径。"""

from __future__ import annotations

import pytest

from evaharness.fingerprint import (
    CONTENT,
    MTIME,
    ChangeDetector,
    InMemoryFingerprintStore,
    SqliteFingerprintStore,
    fingerprint,
)


def test_empty_dir_has_no_entries(tmp_path):
    """空目录是「这里什么都没有」，不是错误——entries 为空、摘要稳定。"""
    empty = tmp_path / "empty"
    empty.mkdir()

    fp = fingerprint([empty])

    assert fp.entries == {}
    assert fp.files == ()
    assert fp.mode == MTIME


def test_missing_path_raises(tmp_path):
    """路径不存在要**响亮地失败**，不能假装它是个空目录。"""
    with pytest.raises(FileNotFoundError):
        fingerprint([tmp_path / "nope"])


def test_unknown_mode_raises(tmp_path):
    (tmp_path / "a.md").write_text("hi", encoding="utf-8")
    with pytest.raises(ValueError, match="不认识的指纹模式"):
        fingerprint([tmp_path], mode="hash")


def test_entries_are_relative_to_the_directory(tmp_path):
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "b.md").write_text("b", encoding="utf-8")

    fp = fingerprint([tmp_path])

    assert fp.files == ("a.md", "sub/b.md")


def test_single_file_key_is_its_name(tmp_path):
    target = tmp_path / "solo.txt"
    target.write_text("x", encoding="utf-8")

    fp = fingerprint([target])

    assert fp.files == ("solo.txt",)


def test_duplicate_keys_raise(tmp_path):
    """两个输入撞到同一个键时点名叫停——否则「哪个文件变了」会指错。"""
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    (one / "same.md").write_text("1", encoding="utf-8")
    (two / "same.md").write_text("2", encoding="utf-8")

    with pytest.raises(ValueError, match="同一个指纹键"):
        fingerprint([one / "same.md", two / "same.md"])


def test_ignore_filters_relative_paths(tmp_path):
    (tmp_path / "keep.md").write_text("k", encoding="utf-8")
    (tmp_path / "skip.tmp").write_text("s", encoding="utf-8")
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    (drafts / "note.md").write_text("d", encoding="utf-8")

    fp = fingerprint([tmp_path], ignore=["*.tmp", "drafts/*"])

    assert fp.files == ("keep.md",)


def test_same_tree_gives_same_digest(tmp_path):
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    assert fingerprint([tmp_path]).digest == fingerprint([tmp_path]).digest


def test_content_mode_survives_touch(tmp_path):
    """content 模式对「内容没变、mtime 变了」不敏感——这正是它相对 mtime 的卖点。"""
    target = tmp_path / "a.md"
    target.write_text("same", encoding="utf-8")
    before = fingerprint([tmp_path], mode=CONTENT)

    target.write_text("same", encoding="utf-8")  # 重写同样的内容

    assert fingerprint([tmp_path], mode=CONTENT).digest == before.digest


def test_mtime_mode_notices_size_change(tmp_path):
    target = tmp_path / "a.md"
    target.write_text("a", encoding="utf-8")
    before = fingerprint([tmp_path], mode=MTIME)

    target.write_text("aaa", encoding="utf-8")

    assert fingerprint([tmp_path], mode=MTIME).digest != before.digest


# --------------------------------------------------------------------------- #
# 变更检测
# --------------------------------------------------------------------------- #


def _write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _diff(store, root, name="tree"):
    return ChangeDetector(store).diff(name, fingerprint([root], mode=CONTENT))


def test_first_run_marks_everything_added(tmp_path):
    _write(tmp_path, "a.md", "a")
    _write(tmp_path, "b.md", "b")

    changes = _diff(InMemoryFingerprintStore(), tmp_path)

    assert changes.added == ("a.md", "b.md")
    assert changes.removed == () and changes.modified == ()
    assert not changes.is_empty


def test_added_removed_modified(tmp_path):
    store = InMemoryFingerprintStore()
    _write(tmp_path, "a.md", "a")
    _write(tmp_path, "keep.md", "keep")
    store.save("tree", fingerprint([tmp_path], mode=CONTENT))

    _write(tmp_path, "a.md", "a-changed")
    (tmp_path / "keep.md").unlink()
    _write(tmp_path, "b.md", "b")

    changes = _diff(store, tmp_path)

    assert changes.added == ("b.md",)
    assert changes.removed == ("keep.md",)
    assert changes.modified == ("a.md",)
    assert changes.changed == ("a.md", "b.md")


def test_no_change_is_empty(tmp_path):
    store = InMemoryFingerprintStore()
    _write(tmp_path, "a.md", "a")
    store.save("tree", fingerprint([tmp_path], mode=CONTENT))

    changes = _diff(store, tmp_path)

    assert changes.is_empty
    assert not ChangeDetector(store).is_changed("tree", fingerprint([tmp_path], mode=CONTENT))


def test_removal_alone_is_detected(tmp_path):
    """删除**不会被漏掉**——整棵树的摘要方案在这里会瞎，按文件的 entries 不会。"""
    store = InMemoryFingerprintStore()
    _write(tmp_path, "gone.md", "x")
    store.save("tree", fingerprint([tmp_path], mode=CONTENT))
    (tmp_path / "gone.md").unlink()

    changes = _diff(store, tmp_path)

    assert changes.removed == ("gone.md",)
    assert ChangeDetector(store).is_changed("tree", fingerprint([tmp_path], mode=CONTENT))


def test_is_changed_on_first_run(tmp_path):
    _write(tmp_path, "a.md", "a")
    assert ChangeDetector(InMemoryFingerprintStore()).is_changed("tree", fingerprint([tmp_path]))


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #


def _roundtrip(store, tmp_path):
    _write(tmp_path, "a.md", "hello")
    fp = fingerprint([tmp_path], mode=CONTENT)
    store.save("kb", fp)

    loaded = store.load("kb")
    assert loaded is not None
    assert loaded.digest == fp.digest
    assert loaded.mode == CONTENT
    assert dict(loaded.entries) == dict(fp.entries)
    assert loaded.paths == fp.paths


def test_memory_store_roundtrip(tmp_path):
    _roundtrip(InMemoryFingerprintStore(), tmp_path)


def test_memory_store_missing_returns_none():
    assert InMemoryFingerprintStore().load("never") is None


def test_sqlite_store_roundtrip(tmp_path):
    store = SqliteFingerprintStore(tmp_path / "nested" / "fp.sqlite")
    try:
        assert store.load("kb") is None
        _roundtrip(store, tmp_path / "src")
    finally:
        store.close()


def test_sqlite_store_overwrites_same_name(tmp_path):
    store = SqliteFingerprintStore(tmp_path / "fp.sqlite")
    src = tmp_path / "src"
    try:
        _write(src, "a.md", "1")
        store.save("kb", fingerprint([src], mode=CONTENT))
        _write(src, "a.md", "2")
        store.save("kb", fingerprint([src], mode=CONTENT))
        loaded = store.load("kb")
        assert loaded is not None
        assert (loaded.paths) == (str(src),)
    finally:
        store.close()


def test_memory_store_close_clears(tmp_path):
    _write(tmp_path, "a.md", "a")
    store = InMemoryFingerprintStore()
    store.save("x", fingerprint([tmp_path]))
    store.close()
    assert store.load("x") is None
