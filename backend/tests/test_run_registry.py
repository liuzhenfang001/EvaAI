"""契约测试：运行中 run 的登记表。"""

import asyncio

import pytest

from app.gateway.run_registry import RunRegistry


async def _never() -> None:
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_cancel_cancels_a_registered_running_task():
    registry = RunRegistry()
    task = asyncio.create_task(_never())
    registry.register("r1", task)

    assert registry.cancel("r1") is True
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_cancel_misses_an_unknown_run():
    registry = RunRegistry()
    assert registry.cancel("nope") is False


@pytest.mark.asyncio
async def test_cancel_misses_an_already_finished_run():
    """pump 刚跑完、还没 ``unregister`` 的那一瞬，取消也不该撒谎「成功了」。"""
    registry = RunRegistry()
    task = asyncio.create_task(asyncio.sleep(0))  # 立即完成
    await task
    registry.register("r1", task)

    assert registry.cancel("r1") is False


@pytest.mark.asyncio
async def test_unregister_makes_the_run_uncancellable():
    registry = RunRegistry()
    task = asyncio.create_task(_never())
    registry.register("r1", task)
    registry.unregister("r1")

    assert registry.cancel("r1") is False
    task.cancel()  # 清理，别留一个挂着的 task
    with pytest.raises(asyncio.CancelledError):
        await task
