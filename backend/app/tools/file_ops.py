"""文件工具：在沙箱的虚拟路径里读写。

**失败一律 raise**，不返回 ``Error: ...`` 字符串。``ToolErrorHandlingMiddleware``（默认链
第 3 项）本来就是干这个的——工具里再兜一次会让失败有**两种**形状，而且它转出来的
ToolMessage 带 ``status="error"``，模型与框架都认得出；一条内容是 ``"Error: ..."`` 的正常
返回值在摘要压缩时与正文没有区别。代价是异常文案要短（中间件截到 500 字符）且**不含宿主
路径**——所以路径相关的话术一律经 :class:`~app.tools.sandbox.Sandbox` 的虚拟路径措辞。

输出与异常用英文：模型侧（系统提示词、中间件的兜底句）本来就是英文，工具跟着走。
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from app.tools.sandbox import Sandbox
from evaharness.agents.middlewares.context import session_id

MAX_READ_CHARS = 16_000
"""``read_file`` 单次返回上限。

按**字符**设限而不是按行数：中文一个字约一个 token，16k 字符 ≈ 16k token，已经吃掉
DeepSeek 64K 上下文的四分之一。超限做**头部**截断（源码与文档的信息在前面），并附上
「用 start_line/end_line 读后段」的可操作提示。
"""

MAX_LS_ENTRIES = 200
MAX_GLOB_RESULTS = 200
MAX_GREP_MATCHES = 100
MAX_GREP_LINE_CHARS = 400
"""单行太长（minified js）会把上下文冲垮，匹配行按这个长度截断。"""

MAX_GREP_FILES = 500
"""扫过这么多文件就停——避免在大树上空转。"""

_DEFAULT_WRITE_BYTES = 80 * 1024
"""非 append 单次写入上限。

一次写太大意味着这次工具调用的 JSON 要一次性流出来，流式 chunk 间隔超时的概率随
payload 增长。超出时给的是**可操作**的错误（分批写 / ``append=True`` / ``str_replace``），
不是一句「太大了」。
"""


def _max_write_bytes() -> int:
    """写入上限。运维旋钮，所以允许环境变量覆盖（DeerFlow 对同一个上限也是这么做的）。"""
    raw = os.getenv("EVA_WRITE_FILE_MAX_BYTES")
    try:
        return int(raw) if raw else _DEFAULT_WRITE_BYTES
    except ValueError:
        return _DEFAULT_WRITE_BYTES


def _sandbox(runtime: ToolRuntime[Any]) -> Sandbox:
    """每次调用现建——不按会话缓存，那就是个无界字典（工具实例跨请求共享）。"""
    return Sandbox(session_id(runtime))


def _require_file(box: Sandbox, target: Path) -> Path:
    """文件必须存在且真的是文件。

    「不存在」与「是个目录」要分开说：模型看到前者该去 ``ls``，看到后者该换个工具。
    合成一句「不是可读文件」它就只剩猜。
    """
    if not target.exists():
        raise ValueError(f"{box.display(target)} does not exist. Use ls to see what is there.")
    if target.is_dir():
        raise ValueError(f"{box.display(target)} is a directory; use ls on it instead.")
    return target


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ValueError(
            f"cannot read {path.name!r} as UTF-8 text — it looks like a binary file. "
            "read_file only handles text."
        ) from None
    except OSError as exc:
        raise ValueError(f"cannot read {path.name!r}: {exc.strerror or type(exc).__name__}") from None


def _iter_files(root: Path, sandbox: Sandbox, pattern: str | None) -> Iterator[Path]:
    """遍历挂载根下的文本文件，**逐条**复核归属（见 :meth:`Sandbox.contains`）。"""
    for candidate in sorted(root.rglob(pattern or "*")):
        if candidate.is_file() and sandbox.contains(candidate):
            yield candidate


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


@tool(parse_docstring=True)
def ls(runtime: ToolRuntime[Any], path: str = "/workspace") -> str:
    """List a directory as an indented tree, up to two levels deep.

    Args:
        path: Virtual directory path — one of /workspace, /input, /output, /skills.
            A relative path is resolved against /workspace.
    """
    box = _sandbox(runtime)
    target = box.resolve(path)
    shown = box.display(target)

    if not target.exists():
        return f"{shown} does not exist yet (nothing has been written there)."
    if target.is_file():
        return f"{shown} is a file ({target.stat().st_size} bytes); use read_file on it."

    lines: list[str] = []
    _walk(target, lines, depth=2, counter=[0])
    if not lines:
        return f"{shown} is empty."
    header = f"{shown}/" if not shown.endswith("/") else shown
    return "\n".join([header, *lines])


def _walk(directory: Path, lines: list[str], *, depth: int, counter: list[int]) -> None:
    """把 `directory` 摊成缩进的行（只渲染名字——树里不需要完整虚拟路径）。"""
    if depth < 1:
        return
    entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    for entry in entries:
        if counter[0] >= MAX_LS_ENTRIES:
            lines.append("… (truncated; list a subdirectory to see more)")
            return
        counter[0] += 1
        indent = "  " * (3 - depth)
        if entry.is_dir():
            lines.append(f"{indent}{entry.name}/")
            _walk(entry, lines, depth=depth - 1, counter=counter)
        else:
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            lines.append(f"{indent}{entry.name} ({size} bytes)")


@tool(parse_docstring=True)
def read_file(runtime: ToolRuntime[Any], path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    """Read a UTF-8 text file, optionally just a line range (1-indexed, inclusive).

    Long files are truncated from the top; use start_line/end_line to read further down.

    Args:
        path: Virtual file path — under /workspace, /input, /output or /skills.
        start_line: First line to return. Defaults to the beginning of the file.
        end_line: Last line to return. Defaults to the end of the file.
    """
    box = _sandbox(runtime)
    target = _require_file(box, box.resolve(path))

    content = _text(target)
    lines = content.splitlines()
    if not lines:
        return f"{box.display(target)} is empty."

    first = max(1, start_line or 1)
    last = min(len(lines), end_line or len(lines))
    if first > last:
        return f"{box.display(target)} has {len(lines)} lines; the requested range is empty."

    body = "\n".join(lines[first - 1 : last])
    head = f"{box.display(target)} (lines {first}-{last} of {len(lines)})\n"
    if len(body) > MAX_READ_CHARS:
        body = body[:MAX_READ_CHARS]
        return head + body + f"\n… (truncated at {MAX_READ_CHARS} chars; use start_line/end_line to read on)"
    return head + body


@tool(parse_docstring=True)
def write_file(runtime: ToolRuntime[Any], path: str, content: str, append: bool = False) -> str:
    """Write text to a file, overwriting it by default. Returns "OK".

    A single non-append call must stay under 80 KB; for bigger content write the first
    part, then extend it with append=True or str_replace.

    Args:
        path: Virtual file path — must be under /workspace or /output.
        content: The text to write.
        append: Append instead of overwriting. Not subject to the size limit.
    """
    box = _sandbox(runtime)
    target = box.resolve(path, write=True)

    size = len(content.encode("utf-8"))
    limit = _max_write_bytes()
    if not append and size > limit:
        raise ValueError(
            f"content is {size} bytes, over the {limit}-byte limit for a single write. "
            "Write the first part, then extend it with append=True or str_replace."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    if append:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(content)
    else:
        target.write_text(content, encoding="utf-8")
    return f"OK ({box.display(target)}, {size} bytes)"


@tool(parse_docstring=True)
def str_replace(
    runtime: ToolRuntime[Any],
    path: str,
    old_str: str,
    new_str: str,
    replace_all: bool = False,
) -> str:
    """Replace a substring in a file. Returns "OK".

    Args:
        path: Virtual file path — must be under /workspace or /output.
        old_str: The text to replace.
        new_str: The replacement text.
        replace_all: Replace every occurrence. When false, old_str must appear exactly
            once — otherwise nothing is changed and the call fails.
    """
    box = _sandbox(runtime)
    target = _require_file(box, box.resolve(path, write=True))

    content = _text(target)
    found = content.count(old_str)
    if found == 0:
        raise ValueError(f"old_str was not found in {box.display(target)}; nothing was changed. Read the file first.")
    if found > 1 and not replace_all:
        raise ValueError(
            f"old_str appears {found} times in {box.display(target)}; nothing was changed. "
            "Give a longer, unique old_str, or pass replace_all=True."
        )

    updated = content.replace(old_str, new_str) if replace_all else content.replace(old_str, new_str, 1)
    target.write_text(updated, encoding="utf-8")
    return f"OK ({box.display(target)}, {found} replaced)"


@tool(parse_docstring=True)
def glob(
    runtime: ToolRuntime[Any],
    pattern: str,
    path: str = "/workspace",
    include_dirs: bool = False,
    max_results: int = 200,
) -> str:
    """Find files under a directory whose path matches a glob pattern.

    Args:
        pattern: Glob pattern relative to the root, e.g. "**/*.py".
        path: Virtual root directory — one of /workspace, /input, /output, /skills.
        include_dirs: Also return matching directories. Defaults to files only.
        max_results: Cap on returned paths.
    """
    flat = pattern.replace("\\", "/")
    if flat.startswith("/") or ".." in flat.split("/"):
        raise ValueError("glob patterns must be relative and may not contain '..'")

    box = _sandbox(runtime)
    root = box.resolve(path)
    cap = max(1, min(int(max_results), MAX_GLOB_RESULTS))
    if not root.exists():
        return f"{box.display(root)} does not exist yet."

    matches: list[str] = []
    for candidate in sorted(root.glob(flat)):
        if candidate.is_dir() and not include_dirs:
            continue
        if not box.contains(candidate):
            continue  # 复核不过（符号链接跳出去了）
        matches.append(box.display(candidate) + ("/" if candidate.is_dir() else ""))
        if len(matches) >= cap:
            matches.append(f"… (capped at {cap})")
            break
    return "\n".join(matches) if matches else f"No matches for {pattern!r} under {box.display(root)}."


@tool(parse_docstring=True)
def grep(
    runtime: ToolRuntime[Any],
    pattern: str,
    path: str = "/workspace",
    glob: str | None = None,
    literal: bool = False,
    case_sensitive: bool = False,
    max_results: int = 100,
) -> str:
    """Search for matching lines inside the text files under a directory.

    Args:
        pattern: A regular expression, or a plain string when literal=True.
        path: Virtual file or directory to search.
        glob: Optional file filter relative to the root, e.g. "**/*.md".
        literal: Treat pattern as a plain string instead of a regular expression.
        case_sensitive: Match case-sensitively.
        max_results: Cap on returned matching lines.
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        matcher = re.compile(re.escape(pattern) if literal else pattern, flags)
    except re.error as exc:
        raise ValueError(f"invalid regular expression: {exc}") from None

    box = _sandbox(runtime)
    root = box.resolve(path)
    if not root.exists():
        return f"{box.display(root)} does not exist yet."

    cap = max(1, min(int(max_results), MAX_GREP_MATCHES))
    targets = [root] if root.is_file() else _iter_files(root, box, glob)
    hits: list[str] = []
    scanned = 0

    for candidate in targets:
        if scanned >= MAX_GREP_FILES or len(hits) >= cap:
            break
        scanned += 1
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(lines, start=1):
            if matcher.search(line):
                text = line[:MAX_GREP_LINE_CHARS]
                hits.append(f"{box.display(candidate)}:{number}: {text}")
                if len(hits) >= cap:
                    break

    if not hits:
        return f"No matches for {pattern!r} under {box.display(root)}."
    if len(hits) >= cap:
        hits.append(f"… (capped at {cap})")
    return "\n".join(hits)


__all__ = ["glob", "grep", "ls", "read_file", "str_replace", "write_file"]
