"""目录 / 文件指纹：只回答「变了没、变哪些」，不决定任何业务动作。

**为什么是一个独立模块**：它是「源变了没」这件通用机制——知识库索引、配置热加载、技能更新
将来都要问同一个问题。所以它**不 import 任何业务模块**，也不认识「索引」「重建」这些词，
只把一组路径压成一个可比较的指纹对象。

**粒度是单个文件**，不是整棵树的摘要：``TreeFingerprint.entries`` 是「相对路径 → 单文件指纹」
的映射，于是增 / 删 / 改都能从两份 entries 的差异里读出来，**删除也不会被漏掉**（整棵树的
摘要一变，你只知道「变了」，不知道是哪个文件——那对增量重建没用）。

两种模式：

- ``"mtime"``：``mtime_ns:size``。快，一次 ``stat`` 即可；代价是有些同步工具 / 检出场景会把
  内容没变的文件也更新 mtime（假阳性 = 多做一次无谓的重建）。
- ``"content"``：文件内容的 ``sha256``。准，但要读整份文件——大库慢。

选哪一种是**策略**，由调用方给。本模块不带默认立场，只把差异如实报出来。
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

MTIME = "mtime"
CONTENT = "content"
MODES = (MTIME, CONTENT)

Chunk = tuple[str, str]
"""``(相对路径, 单文件指纹)``。"""


@dataclass(frozen=True, slots=True)
class TreeFingerprint:
    """一组路径在某一时刻的指纹快照。

    ``paths`` 是**输入**（原样记下，便于调试「这份指纹是怎么来的」）；``entries`` 是
    「相对路径 → 单文件指纹」；``digest`` 是 entries 的一个整体摘要，用来做一次性的
    「变没变」判断（不必逐条比）。``mode`` 记下用哪种算法算的——两次指纹若 mode 不同，
    所有单文件指纹都会不同，调用方据此能看出「这是换了算法，不是文件真变了」。
    """

    paths: tuple[str, ...]
    mode: str
    digest: str
    entries: Mapping[str, str] = field(default_factory=dict)

    @property
    def files(self) -> tuple[str, ...]:
        """entries 的全部相对路径（有序）。"""
        return tuple(sorted(self.entries))


def fingerprint(
    paths: Iterable[str | os.PathLike[str]],
    *,
    ignore: Iterable[str] = (),
    mode: str = MTIME,
) -> TreeFingerprint:
    """算一组路径的指纹。

    Args:
        paths: 文件或目录。目录会被递归展开。
        ignore: fnmatch 通配符，**匹配相对路径**。注意 ``*`` 在 fnmatch 里能跨 ``/``，
            所以 ``*.tmp`` 会命中任意深度的 ``.tmp``。
        mode: ``"mtime"``（快）或 ``"content"``（准）。

    Returns:
        :class:`TreeFingerprint`。``entries`` 的键对**目录输入**是该目录内的相对路径，
        对**文件输入**是文件名。

    Raises:
        ValueError: 不认识的 ``mode``，或两个输入产生了同一个键（键必须唯一，否则
            「哪个文件变了」会指错）。
        FileNotFoundError: 某个输入路径不存在——**响亮地失败**，不假装它是个空目录。
            空目录与不存在的区别是真实的：前者是「这里什么都没有」，后者是「你指错地方了」。
    """
    if mode not in MODES:
        raise ValueError(f"不认识的指纹模式：{mode!r}（只支持 {MODES}）")
    ignores = tuple(ignore)

    chunks: list[Chunk] = []
    seen: set[str] = set()
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            raise FileNotFoundError(f"路径不存在：{path}")
        items = _iter_dir(path) if path.is_dir() else [(path.name, path)]
        for key, file in items:
            if _ignored(key, ignores):
                continue
            if key in seen:
                raise ValueError(f"两个输入产生了同一个指纹键：{key!r}（相对路径必须唯一）")
            seen.add(key)
            chunks.append((key, _file_fingerprint(file, mode)))

    chunks.sort()
    return TreeFingerprint(
        paths=tuple(str(Path(p)) for p in paths),
        mode=mode,
        digest=_digest(chunks),
        entries=dict(chunks),
    )


def _iter_dir(root: Path) -> Iterator[Chunk]:
    """递归展开一个目录，产出 ``(相对路径, 文件 Path)``。

    不跟随符号链接 / junction——它们是现成的逃逸与环手法，而指纹只该覆盖真实落在这棵树里
    的文件。空目录在这里自然产出 0 条（不是错误）。
    """
    stack = [root]
    while stack:
        current = stack.pop()
        for entry in sorted(current.iterdir()):
            if _is_link(entry):
                continue
            if entry.is_dir():
                stack.append(entry)
            elif entry.is_file():
                yield str(entry.relative_to(root)).replace(os.sep, "/"), entry


def _file_fingerprint(path: Path, mode: str) -> str:
    if mode == CONTENT:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    stat = path.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _digest(chunks: list[Chunk]) -> str:
    """entries 的整体摘要：路径与单文件指纹都进哈希，且用长度无关的分隔固定边界。"""
    hasher = hashlib.sha256()
    for key, value in chunks:
        hasher.update(key.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(value.encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()


def _ignored(key: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(key, pattern) for pattern in patterns)


def _is_link(path: Path) -> bool:
    """符号链接或 Windows junction；不跟随它们（理由同 :mod:`evaharness.sandbox.paths`）。"""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


__all__ = ["CONTENT", "MODES", "MTIME", "TreeFingerprint", "fingerprint"]
