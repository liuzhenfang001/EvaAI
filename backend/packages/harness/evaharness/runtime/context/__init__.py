"""运行上下文：一次运行对图里的中间件与工具自称的样子，以及取它的三个访问器。

- :mod:`~evaharness.runtime.context.contract` —— :class:`RunContext`（运行身份 + 沙箱注入）；
- :mod:`~evaharness.runtime.context.accessors` —— ``session_id`` / ``thread_bucket`` / ``sandbox_layout``；
- :mod:`~evaharness.runtime.context.options` —— :class:`RunOptions`（这一次运行的旋钮）。

**这一层不 import 任何 L2 机制包**（只有 ``SandboxLayout`` 的类型标注走 ``TYPE_CHECKING``），
所以工具包可以放心依赖它——这正是它从 ``agents`` 里搬出来的理由（ADR-0024）。
"""

from __future__ import annotations

from evaharness.runtime.context.accessors import (
    FALLBACK_BUCKET,
    SAFE_SESSION_ID,
    sandbox_layout,
    session_id,
    thread_bucket,
)
from evaharness.runtime.context.contract import RunContext
from evaharness.runtime.context.options import RunOptions, ThinkingLevel

__all__ = [
    "FALLBACK_BUCKET",
    "SAFE_SESSION_ID",
    "RunContext",
    "RunOptions",
    "ThinkingLevel",
    "sandbox_layout",
    "session_id",
    "thread_bucket",
]
