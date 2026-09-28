"""SQLite checkpoint 持久化与保留策略。

持久化的是**每个 ReAct 任务图内部的 state**（按 ``run_id:task_id`` 分 thread），
不是编排进度——编排进度活在 Python 栈帧里（见 ADR-0006 / ADR-0007）。所以这里
买不到「重启后接着跑编排」，买到的是任务级状态的durability与 T5 的地基。

保留策略靠**从 checkpoint id 反解时间**：langgraph 的 ``checkpoints`` 表只有
``thread_id / checkpoint_ns / checkpoint_id / …``，没有时间戳列。checkpoint id 是
UUIDv6，前 60 位是 100ns 时间戳——但 UUIDv6 的 version nibble 插在时间字段中间，
必须按 48 位高位 + 12 位低位拼接，不能简单地取一段掩码。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 仅为类型标注，运行期不导入
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

# UUID 纪元（1582-10-15）与 Unix 纪元之间的 100ns 间隔数。
_UUID_EPOCH_100NS = 0x01B21DD213814000
_100NS_PER_MS = 10_000


def checkpoint_timestamp_ms(checkpoint_id: Any) -> int | None:
    """从 UUIDv6 形式的 checkpoint id 取 Unix 毫秒；不是 v6 或无法解析时返回 ``None``。

    返回 ``None`` 是给清理逻辑的「跳过」信号——宁可不删，不可误删。
    """
    try:
        parsed = uuid.UUID(str(checkpoint_id))
    except (ValueError, AttributeError, TypeError):
        return None
    if parsed.version != 6:
        return None
    timestamp_100ns = ((parsed.int >> 80) << 12) | ((parsed.int >> 64) & 0xFFF)
    return (timestamp_100ns - _UUID_EPOCH_100NS) // _100NS_PER_MS


@asynccontextmanager
async def make_sqlite_checkpointer(db_path: str | Path) -> AsyncIterator[AsyncSqliteSaver]:
    """打开（必要时创建）SQLite checkpoint 库，并建好表。

    ``AsyncSqliteSaver.__init__`` 会调 ``asyncio.get_running_loop()``，因此**必须在
    协程内构造**；长生命周期的持有方（如 FastAPI lifespan）用 ``AsyncExitStack``
    包住这个上下文管理器即可。
    """
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        await saver.setup()  # 显式建表，让配置错误在启动时就暴露
        yield saver


async def prune_checkpoints(saver: Any, *, retention_days: float) -> int:
    """删除「最后活动时间早于 ``retention_days`` 天」的 thread，返回删除的 thread 数。

    thread 的最后活动时间取它最新的那条 checkpoint。``retention_days <= 0`` 表示
    不清理（而不是「全清」）——配置写错时倾向于保留数据。
    """
    if retention_days <= 0:
        return 0

    cutoff_ms = int(time.time() * 1000) - int(retention_days * 86_400_000)
    newest_ms: dict[str, int | None] = {}
    # alist 按 checkpoint_id 降序，所以每个 thread 的第一次出现就是它最新的 checkpoint。
    async for checkpoint_tuple in saver.alist(None):
        thread_id = str(checkpoint_tuple.config["configurable"]["thread_id"])
        if thread_id in newest_ms:
            continue
        newest_ms[thread_id] = checkpoint_timestamp_ms(checkpoint_tuple.checkpoint.get("id"))

    stale = [thread_id for thread_id, latest in newest_ms.items() if latest is not None and latest < cutoff_ms]
    for thread_id in stale:
        await saver.adelete_thread(thread_id)
    return len(stale)


__all__ = ["checkpoint_timestamp_ms", "make_sqlite_checkpointer", "prune_checkpoints"]
