"""图流的一条载荷 → :class:`~evaharness.runtime.event.contract.RunEvent`。

``agent.astream(..., stream_mode=["messages", "values", "custom"])`` 吐出来的是**三种形状
混在一条流里**，这里是唯一认它们的地方：

- ``messages``：载荷是嵌套元组——外层 ``(mode, payload)``、``messages`` 的 payload 自身又是
  ``(chunk, metadata)``。翻成 :class:`MessagesEvent`。
- ``custom``：工具经 :attr:`~langchain.tools.ToolRuntime.stream_writer` 送的**载荷**
  （见 :mod:`evaharness.runtime.event.custom`）。翻成它声明的那条事件，**认不出来的一律丢掉**。
- ``values``：状态快照，**不是事件**——它由调用方留着，收尾时取正文
  （:func:`~evaharness.runtime.state.final_text`）。所以本模块对它返回 ``None``。

把这段从运行入口里提出来（T19，见 ADR-0024）是为了让 ``run_agent`` 只剩「接线 + 终态」，
而事件形状的知识集中在一处。

.. warning::
   原实现里这段是 ``if messages / elif custom / else(values)`` 的**顺序敏感**结构，注释记着：
   custom 必须排在 else 之前，否则工具载荷会被当成 state 快照覆盖 ``last_state``，症状是
   ``run_end.output`` 莫名其妙变成一段工具载荷。调用方现在照 :func:`produces_event` 做
   **正向判定**，那个陷阱在结构上就不存在了——但别把这里的「认不出来返回 ``None``」改成
   「兜底当成快照」，那正是原注释在防的事。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from evaharness.runtime.event.contract import (
    FilesPresentedEvent,
    MessagesEvent,
    PresentedFile,
    RunEvent,
)
from evaharness.runtime.event.custom import PRESENT_FILES_KIND

logger = logging.getLogger(__name__)

_EVENT_MODES = frozenset({"messages", "custom"})
"""会产出事件的流模式。其余（``values``）是状态快照，不归本模块。"""


def produces_event(stream_kind: str) -> bool:
    """这个流模式会不会产出事件（反过来说：是不是那次 ``values`` 快照）。

    调用方用它做**正向判定**，而不是在 :func:`translate` 返回 ``None`` 时反推要不要留 state
    ——认不出来的自定义载荷同样返回 ``None``，那两种 ``None`` 不能混为一谈（见模块 docstring
    的 warning）。把模式表放在这里而不是调用方硬编码，是为了它只有一份。
    """
    return stream_kind in _EVENT_MODES


def translate(stream_kind: str, data: Any, *, lane: str) -> RunEvent | None:
    """把图流的一条载荷翻成事件；不是事件的（``values``、不认识的自定义载荷）返回 ``None``。

    Args:
        stream_kind: ``astream`` 给的模式名（``"messages"`` / ``"values"`` / ``"custom"``）。
        data: 该模式对应的载荷，形状见模块 docstring。
        lane: 这一轮运行在事件流里的泳道 id。**由调用方给**，因为这个模块不该知道谁在跑
            ——:func:`~evaharness.runtime.run_agent` 把它当参数收，默认值是
            :data:`~evaharness.runtime.event.contract.DEFAULT_LANE`。
    """
    if stream_kind == "messages":
        return _message_event(data, lane=lane)
    if stream_kind == "custom":
        return _custom_event(data, lane=lane)
    return None


def _message_event(data: Any, *, lane: str) -> MessagesEvent | None:
    """``messages`` 载荷 ``(chunk, metadata)`` → :class:`MessagesEvent`。

    ``hide_from_ui`` 的消息**一律丢弃**（返回 ``None``）。当前它们不会出现在 ``messages``
    流里——``create_agent`` 的中间件节点是 ``trace=False``，节点输出不进消息流。但那是内部
    实现细节；前端历史完全建立在 journal 的 message 桶上，一旦上游改变行为，一条内部注入
    消息就会变成用户可见的对话气泡。这里按「标记语义」再挡一次，代价只有一次字典查询。
    """
    chunk, metadata = data
    if (getattr(chunk, "additional_kwargs", None) or {}).get("hide_from_ui"):
        return None
    return MessagesEvent(
        task_id=lane,
        chunk=chunk.model_dump(mode="json"),
        metadata=metadata if isinstance(metadata, dict) else {},
    )


def _custom_event(payload: Any, *, lane: str) -> RunEvent | None:
    """工具送出的自定义载荷 → ``RunEvent``；不认识的一律返回 ``None``。

    **白名单**，不是「有 ``type`` 字段就照着构造」：自定义流是开放的，任何工具都能往里写
    任意 JSON——照单全收的话，一个工具就能伪造 ``run_end`` 把本轮运行提前结束掉。所以这里
    只认自己定义过的 kind，映射关系写死在代码里。

    载荷不合法（缺字段、类型不对）时记一条警告后跳过，**不抛**：与 journal 记账失败同一个
    立场——可观测/附属通道出问题不该把用户的这一轮跑挂。
    """
    if not isinstance(payload, Mapping) or payload.get("kind") != PRESENT_FILES_KIND:
        return None
    files = payload.get("files") or []
    if not files:
        return None
    try:
        return FilesPresentedEvent(
            task_id=lane,
            files=[PresentedFile.model_validate(file) for file in files],
        )
    except ValidationError:
        logger.warning("工具送出的文件载荷不合法，已忽略", exc_info=True)
        return None


__all__ = ["produces_event", "translate"]
