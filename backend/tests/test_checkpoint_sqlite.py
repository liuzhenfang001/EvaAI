"""契约测试：SQLite checkpoint 持久化与保留策略。

保留策略要靠「从 checkpoint id 反解时间」实现——langgraph 的 ``checkpoints`` 表
**没有时间戳列**。checkpoint id 是 UUIDv6（时间有序），但 version nibble 卡在
时间字段中间，位运算错一位就会让清理逻辑静默地全删或全不删，所以这个纯函数
必须单独钉死。
"""

import time

import pytest
from langgraph.checkpoint.base.id import uuid6

from evaharness.checkpoint.sqlite_saver import (
    checkpoint_timestamp_ms,
    delete_session_checkpoints,
    make_sqlite_checkpointer,
    prune_checkpoints,
    task_thread_id,
)

_UUID_EPOCH_100NS = 0x01B21DD213814000


def _uuid6_at(unix_ms: int) -> str:
    """按 UUIDv6 布局手工拼一个指定时刻的 id（用于造「旧」数据）。

    不能借道 ``uuid.UUID(version=6)``：标准库只认 version 1–5，langgraph 是自己
    bundle 了一份 v6 实现。这里直接按 RFC 布局拼字符串：48 位时间高位 + 版本位
    + 12 位时间低位 + 变体位/时钟序列 + 48 位节点。
    """
    timestamp = unix_ms * 10_000 + _UUID_EPOCH_100NS
    time_high = (timestamp >> 12) & 0xFFFFFFFFFFFF
    time_low = timestamp & 0x0FFF
    hexs = f"{time_high:012x}" + "6" + f"{time_low:03x}" + "8000" + "0" * 12
    return f"{hexs[0:8]}-{hexs[8:12]}-{hexs[12:16]}-{hexs[16:20]}-{hexs[20:32]}"


# --------------------------------------------------------------------------- #
# checkpoint_timestamp_ms
# --------------------------------------------------------------------------- #


async def _write_thread(saver, thread_id: str, checkpoint_id: str) -> None:
    """直接往 saver 里塞一条最小可用的 checkpoint。"""
    await saver.aput(
        {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
        {
            "v": 1,
            "id": checkpoint_id,
            "ts": "",
            "channel_values": {},
            "channel_versions": {},
            "versions_seen": {},
            "pending_sends": [],
        },
        {"source": "loop", "step": 0, "parents": {}},
        {},
    )


async def _threads(saver) -> set[str]:
    return {tup.config["configurable"]["thread_id"] async for tup in saver.alist(None)}


def test_timestamp_of_fresh_checkpoint_id_is_now():
    now_ms = int(time.time() * 1000)
    assert abs(checkpoint_timestamp_ms(str(uuid6())) - now_ms) < 60_000


def test_timestamp_roundtrips_synthetic_ids():
    for offset_ms in (0, 1_000, 1_700_000_000_000):
        assert checkpoint_timestamp_ms(_uuid6_at(offset_ms)) == offset_ms


def test_timestamp_orders_old_before_new():
    older = checkpoint_timestamp_ms(_uuid6_at(1_000_000))
    newer = checkpoint_timestamp_ms(_uuid6_at(2_000_000))
    assert older < newer


@pytest.mark.parametrize("value", ["", "not-a-uuid", "1f1bb23f-508d-11a8-8001-ac10f37a9cc4", None, 42])
def test_timestamp_returns_none_for_non_v6_input(value):
    """v1 id 与垃圾输入都返回 None——清理逻辑据此跳过，绝不误删。"""
    assert checkpoint_timestamp_ms(value) is None


# --------------------------------------------------------------------------- #
# make_sqlite_checkpointer
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_make_sqlite_checkpointer_creates_tables_and_persists(tmp_path):
    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage, HumanMessage
    from plan_exec_fakes import ToolCapableFake

    db_path = tmp_path / "nested" / "checkpoints.sqlite"

    async with make_sqlite_checkpointer(db_path) as saver:
        graph = create_agent(
            model=ToolCapableFake(messages=iter([AIMessage(content="答案")])),
            tools=[],
            checkpointer=saver,
        )
        async for _ in graph.astream(
            {"messages": [HumanMessage("hi")]},
            stream_mode="values",
            config={"configurable": {"thread_id": "run-1:t1"}},
        ):
            pass
        threads = {tup.config["configurable"]["thread_id"] async for tup in saver.alist(None)}

    assert threads == {"run-1:t1"}
    assert db_path.exists(), "父目录不存在时也要能建库"


@pytest.mark.asyncio
async def test_data_survives_reopening_the_database(tmp_path):
    """持久化的意义就在这里：连接关掉再开，数据还在。"""
    db_path = tmp_path / "checkpoints.sqlite"

    async with make_sqlite_checkpointer(db_path) as saver:
        await _write_thread(saver, "run-1:t1", str(uuid6()))

    async with make_sqlite_checkpointer(db_path) as reopened:
        assert await _threads(reopened) == {"run-1:t1"}


# --------------------------------------------------------------------------- #
# prune_checkpoints
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_prune_removes_threads_older_than_retention(tmp_path):
    async with make_sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        await _write_thread(saver, "old:t1", _uuid6_at(int(time.time() * 1000) - 10 * 86_400_000))
        await _write_thread(saver, "new:t1", str(uuid6()))

        deleted = await prune_checkpoints(saver, retention_days=7)

        assert deleted == 1
        assert await _threads(saver) == {"new:t1"}


@pytest.mark.asyncio
async def test_prune_keeps_recent_threads(tmp_path):
    async with make_sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        await _write_thread(saver, "new:t1", str(uuid6()))

        assert await prune_checkpoints(saver, retention_days=7) == 0
        assert await _threads(saver) == {"new:t1"}


@pytest.mark.asyncio
async def test_prune_uses_the_newest_checkpoint_of_a_thread(tmp_path):
    """一个 thread 有很多 checkpoint，活跃时间看最新那个。"""
    async with make_sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        now_ms = int(time.time() * 1000)
        await _write_thread(saver, "mixed:t1", _uuid6_at(now_ms - 30 * 86_400_000))
        await _write_thread(saver, "mixed:t1", str(uuid6()))

        assert await prune_checkpoints(saver, retention_days=7) == 0
        assert await _threads(saver) == {"mixed:t1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("retention_days", [0, -1])
async def test_prune_is_a_noop_for_non_positive_retention(tmp_path, retention_days):
    async with make_sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        await _write_thread(saver, "old:t1", _uuid6_at(1_000_000))

        assert await prune_checkpoints(saver, retention_days=retention_days) == 0
        assert await _threads(saver) == {"old:t1"}


@pytest.mark.asyncio
async def test_prune_skips_threads_with_unparsable_checkpoint_ids(tmp_path):
    """反解不出时间的 thread 一律留下——宁可不删，不可误删。"""
    async with make_sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        await _write_thread(saver, "weird:t1", "not-a-uuid")

        assert await prune_checkpoints(saver, retention_days=1e-9) == 0
        assert await _threads(saver) == {"weird:t1"}


# --------------------------------------------------------------------------- #
# task_thread_id 与按会话清理
# --------------------------------------------------------------------------- #


def test_task_thread_id_is_session_run_task():
    """三段都要：任务区分同一次运行内的多个任务，运行区分同一会话的多次运行，
    会话让血缘可见并让「删会话」能按前缀连带清理。"""
    assert task_thread_id("th-1", "run-1", "t2") == "th-1:run-1:t2"


@pytest.mark.asyncio
async def test_delete_session_checkpoints_removes_only_that_session(tmp_path):
    async with make_sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        await _write_thread(saver, task_thread_id("th-1", "run-1", "t1"), _uuid6_at(1_700_000_000_000))
        await _write_thread(saver, task_thread_id("th-1", "run-1", "t2"), _uuid6_at(1_700_000_000_000))
        await _write_thread(saver, task_thread_id("th-2", "run-1", "t1"), _uuid6_at(1_700_000_000_000))

        deleted = await delete_session_checkpoints(saver, thread_id="th-1")

        assert deleted == 2
        assert await _threads(saver) == {"th-2:run-1:t1"}


@pytest.mark.asyncio
async def test_delete_session_checkpoints_does_not_touch_a_longer_session_id(tmp_path):
    """前缀带分隔符——删 ``abc`` 不能误伤 ``abcd``。"""
    async with make_sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        await _write_thread(saver, task_thread_id("abc", "run-1", "t1"), _uuid6_at(1_700_000_000_000))
        await _write_thread(saver, task_thread_id("abcd", "run-1", "t1"), _uuid6_at(1_700_000_000_000))

        await delete_session_checkpoints(saver, thread_id="abc")

        assert await _threads(saver) == {"abcd:run-1:t1"}


@pytest.mark.asyncio
async def test_delete_session_checkpoints_is_a_noop_for_unknown_sessions(tmp_path):
    async with make_sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        await _write_thread(saver, task_thread_id("th-1", "run-1", "t1"), _uuid6_at(1_700_000_000_000))

        assert await delete_session_checkpoints(saver, thread_id="th-none") == 0
        assert await _threads(saver) == {"th-1:run-1:t1"}
