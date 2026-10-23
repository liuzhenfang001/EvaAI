"""记忆端点：看 / 增 / 改 / 删长期记忆。

**为什么要有这一组路由**：记忆是模型自己写进库的，写错了（记岔了、记成了猜测、把一次性的
任务细节当成了长期偏好）用户必须能**自己**抹掉——否则唯一的修法是拿 sqlite 客户端去改库，
那等于这个功能没有出口。所以这里给的是完整的增删改查，而不只是只读。

**没开长期记忆时这一组仍然挂着**，只是每个端点都返回 409：路由是静态注册的，而
``memory.enabled`` 是运行期配置——403/409 比 404 诚实，它说的是「这个功能没开」而不是
「没有这个端点」。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from evaharness.memory import FACT_CATEGORIES, Fact, ProfileBlock
from evaharness.memory.store import DuplicateFact, MemoryStore, UnknownFact

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memory", tags=["memory"])


class FactCreateRequest(BaseModel):
    """手工记一条。字段与 ``memory_save`` 工具同口径。"""

    content: str
    category: str = "context"
    confidence: float = 0.7


class FactPatchRequest(BaseModel):
    """改一条的局部字段。三个都是可选——只给要改的那个。"""

    content: str | None = None
    category: str | None = None
    confidence: float | None = None


class MemoryResponse(BaseModel):
    """记忆的完整投影：六块画像 + 全部生效事实。"""

    profile: list[ProfileBlock]
    facts: list[Fact]
    categories: list[str]


class MemoryStatusResponse(BaseModel):
    """给页头看的：这个部署有没有开长期记忆、里面有多少东西。"""

    enabled: bool
    facts: int
    profile_blocks: int


def _store(request: Request) -> MemoryStore:
    """取存储；没开长期记忆就 409（见模块说明：不是 404）。"""
    runtime = getattr(request.app.state, "memory", None)
    if runtime is None:
        raise HTTPException(status_code=409, detail="长期记忆未启用（memory.enabled=false）")
    return runtime.store


def _bad_request(exc: Exception) -> HTTPException:
    """存储抛的校验错 → 400。它们说的都是「你给的字段不对」，不是服务端出了问题。"""
    return HTTPException(status_code=400, detail=str(exc))


def _duplicate(exc: DuplicateFact) -> HTTPException:
    """内容重复 → 409：请求本身合法，是与现状冲突。"""
    return HTTPException(status_code=409, detail=str(exc))


def _unknown(fact_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"没有这条记忆：{fact_id}")


@router.get("")
async def get_memory(request: Request) -> MemoryResponse:
    """读出全部记忆。"""
    store = _store(request)
    return MemoryResponse(
        profile=store.profile(),
        facts=store.facts(),
        categories=sorted(FACT_CATEGORIES),
    )


@router.get("/status")
async def get_status(request: Request) -> MemoryStatusResponse:
    """这个部署开没开、里里有多少——**没开也能问**，所以它不用 :func:`_store`。"""
    runtime: Any = getattr(request.app.state, "memory", None)
    if runtime is None:
        return MemoryStatusResponse(enabled=False, facts=0, profile_blocks=0)
    return MemoryStatusResponse(
        enabled=True,
        facts=len(runtime.store.facts()),
        profile_blocks=len(runtime.store.profile()),
    )


@router.post("/facts", status_code=201)
async def create_fact(body: FactCreateRequest, request: Request) -> Fact:
    """手工记一条（用户主动说「记住这个」时，页面上也能直接加）。"""
    store = _store(request)
    try:
        return store.add_fact(content=body.content, category=body.category, confidence=body.confidence)
    except DuplicateFact as exc:
        raise _duplicate(exc) from exc
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.patch("/facts/{fact_id}")
async def update_fact(fact_id: str, body: FactPatchRequest, request: Request) -> Fact:
    """改一条。三个字段都可选，一个都不给就是空操作（仍会校验这条存不存在）。"""
    store = _store(request)
    try:
        return store.update_fact(
            fact_id,
            content=body.content,
            category=body.category,
            confidence=body.confidence,
        )
    except UnknownFact as exc:
        raise _unknown(fact_id) from exc
    except DuplicateFact as exc:
        raise _duplicate(exc) from exc
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.delete("/facts/{fact_id}", status_code=204)
async def delete_fact(fact_id: str, request: Request) -> None:
    """删一条。**幂等**：本来就没有也返回 204——删完页面会刷新列表，竞态下不该报错。"""
    _store(request).forget_fact(fact_id)


@router.delete("", status_code=204)
async def clear_memory(request: Request) -> None:
    """清空全部记忆（画像、事实、水位线）。不可逆——**前端必须二次确认**。

    走 ``runtime.clear()`` 而不是 ``store.clear()``：它还会让队列里待处理与**飞行中**的抽取
    作废。只清库的话，30 秒去抖窗口内那次抽取会把用户刚抹掉的东西写回来。
    """
    runtime = getattr(request.app.state, "memory", None)
    if runtime is None:
        raise HTTPException(status_code=409, detail="长期记忆未启用（memory.enabled=false）")
    await runtime.clear()
