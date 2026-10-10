"""会话（thread）端点：列表、重命名、删除，以及对话与运行记录的取回。

**这是「回放」的服务端一半**：前端切换会话时拉 ``/messages`` 重建对话、拉最近一次运行
的 ``/journal`` 重建任务面板，两次都喂给与实时**同一个**投影函数（前端 runState）。

取代了 T3 的 ``/runs``——T7 起一切都挂在会话下（参照项目也是 thread-scoped）。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.tools import docker_sandbox
from evaharness.checkpoint import delete_session_checkpoints
from evaharness.journal.stores import JournalEntry, JournalThread

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/threads", tags=["threads"])


class RenameThreadRequest(BaseModel):
    """重命名请求体。"""

    title: str


def _not_found(thread_id: str) -> HTTPException:
    # 非法 id（会被 store 拒绝）与不存在的会话都返回 404——对调用方而言两者没有
    # 区别，都不该拿到 500。
    return HTTPException(status_code=404, detail=f"没有这个会话：{thread_id}")


@router.get("")
async def list_threads(request: Request) -> list[JournalThread]:
    """列出全部会话，最近的在前。"""
    return await request.app.state.journal_store.threads()


@router.patch("/{thread_id}")
async def rename_thread(thread_id: str, body: RenameThreadRequest, request: Request) -> JournalThread:
    """给会话起一个自定义标题（覆盖从首个请求推导出来的那个）。"""
    store = request.app.state.journal_store
    await _load_or_404(thread_id, request)

    await store.rename_thread(thread_id, body.title)
    return next(thread for thread in await store.threads() if thread.thread_id == thread_id)


@router.delete("/{thread_id}", status_code=204)
async def delete_thread(thread_id: str, request: Request) -> None:
    """**真删**该会话的全部记录（ADR-0009：不做回收站）。

    连带清掉它的**任务级 checkpoint**——否则 journal 真删了、checkpoint 还留着，要等
    保留策略（默认 7 天）才扫得到，「删除」就名不副实。清理失败只记日志：会话本身
    已经没了，为一次收尾失败回一个错误只会让人以为没删掉。

    幂等：已经不存在也返回 204——前端删完会刷新列表，竞态下不该报错。
    """
    try:
        await request.app.state.journal_store.delete_thread(thread_id)
    except ValueError as exc:
        raise _not_found(thread_id) from exc
    await _delete_checkpoints(request, thread_id)
    await _delete_sandbox_container(request, thread_id)


async def _delete_checkpoints(request: Request, thread_id: str) -> None:
    checkpointer = getattr(request.app.state, "checkpointer", None)
    if checkpointer is None:
        return
    try:
        deleted = await delete_session_checkpoints(checkpointer, thread_id=thread_id)
    except Exception:
        logger.warning("清理会话 %s 的 checkpoint 失败", thread_id, exc_info=True)
        return
    if deleted:
        logger.info("已清理会话 %s 的 %d 个任务级 checkpoint", thread_id, deleted)


async def _delete_sandbox_container(request: Request, thread_id: str) -> None:
    """连带删掉这个会话的沙箱容器（没启用 bash 的部署根本没有容器）。

    容器是这个会话的**运行状态**，不是用户的文件——ADR-0012 那条「删会话不删文件」的债不适用
    于它。失败只记日志：会话本身已经没了，为一次收尾失败回错误只会让人以为没删掉。
    """
    if not getattr(request.app.state, "bash_enabled", False):
        return
    try:
        removed = await asyncio.to_thread(docker_sandbox.remove_session_container, thread_id)
    except Exception:
        logger.warning("清理会话 %s 的沙箱容器失败", thread_id, exc_info=True)
        return
    if removed:
        logger.info("已删除会话 %s 的沙箱容器", thread_id)


async def _load_or_404(thread_id: str, request: Request) -> list[JournalEntry]:
    try:
        entries = await request.app.state.journal_store.load(thread_id)
    except ValueError as exc:
        raise _not_found(thread_id) from exc
    if not entries:
        raise _not_found(thread_id)
    return entries


@router.get("/{thread_id}/messages")
async def list_thread_messages(thread_id: str, request: Request) -> list[JournalEntry]:
    """对话投影：该会话里 ``category == "message"`` 的事件，按行序（即由早到晚）。

    聊天区要的「用户说了什么 + 每个任务产出了什么 + 任务成没成」全在这个桶里。
    ``category`` 是消费者路由桶而非排他分类——面板那条投影要的是**全部**事件。
    """
    entries = await _load_or_404(thread_id, request)
    return [entry for entry in entries if entry.event.category == "message"]


@router.get("/{thread_id}/journal")
async def get_thread_journal(thread_id: str, request: Request) -> list[JournalEntry]:
    """该会话**全部运行**的完整事件，按行序（即由早到晚）。

    前端切换会话时拉这一个端点就够了：把整条流喂给与实时**同一个** reducer，对话与
    面板一起重建。这样「历史与实时逐字一致」是由构造保证的，不需要在前端做
    「对话投影 + 面板投影」的拼接——那两段本来就有重叠（``run_start`` 与 ``messages``
    同时在两个桶里），分开拉再依次喂会让用户气泡重复、时间线顺序错乱。
    """
    return await _load_or_404(thread_id, request)
