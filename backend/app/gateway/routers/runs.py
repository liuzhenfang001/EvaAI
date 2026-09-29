"""运行记录端点：列出历史运行、取回某次运行的完整 RunJournal。

这是「回放」的服务端一半：前端拿到 ``run_id``（随 ``plan`` 事件下发）后，把整条
事件流拉回来喂给与实时相同的投影函数，就能重建计划、时间线与气泡。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from evaharness.journal.stores import JournalEntry, JournalRun

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("")
async def list_runs(request: Request) -> list[JournalRun]:
    """列出已有运行，最近的在前。"""
    return await request.app.state.journal_store.runs()


@router.get("/{run_id}/journal")
async def get_run_journal(run_id: str, request: Request) -> list[JournalEntry]:
    """取回一次运行的全部事件记录（按 seq 升序）。

    非法 run_id（会被 store 拒绝）与不存在的 run 都返回 404——对调用方而言两者
    没有区别，都不该拿到 500。
    """
    try:
        entries = await request.app.state.journal_store.load(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"没有这个运行记录：{run_id}") from exc
    if not entries:
        raise HTTPException(status_code=404, detail=f"没有这个运行记录：{run_id}")
    return entries
