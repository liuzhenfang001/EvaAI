"""运行上下文：一次运行在图里对中间件与工具**自称**的样子。

它是「此刻是哪次运行」的唯一权威来源——中间件靠它分桶、工具靠它决定文件落在哪个会话目录。
传法只有一条：``config`` 走 checkpoint 坐标、``context`` 走这个对象，两者由
:func:`evaharness.runtime.run_agent` 同时给出。

.. note::
   原叫 ``LeadContext``、住在 ``agents/lead_agent/agent.py``（T19 搬来这里，见 ADR-0024）。
   改名与搬家的理由都有实证：子 agent 复用同一个类型（只是坐标不同），所以它本来就不是
   「lead 的上下文」；而它住在 ``agents`` 里的后果是**每个要用文件工具的包都得 import
   ``agents``**——``sandbox`` / ``knowledge`` 都是这么被拖下水的。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RunContext:
    """图运行期的上下文：中间件与工具靠它拿到「此刻是哪次运行」。

    **会话与 checkpoint 坐标是两个不同的东西**，这里刻意分成两个字段——把它们混成
    一个（T7 之前反复踩过）会导致「检查点写在 A、循环检测记在 B」这类难查的症状：

    - ``session_id``：用户眼里的一段对话（``th-1``）。
    - ``thread_id``：**实际传给 checkpointer 的坐标**（``th-1:lead``）。它必须叫这个名字、
      且必须就是坐标本身——``LoopDetectionMiddleware`` 与 ``TerminalResponseMiddleware``
      读的都是 ``context["thread_id"]``，并按它分桶。
    - ``run_id``：本轮运行的标识。

    子 agent 的图复用同一个类型，只是 ``thread_id`` 换成它自己的运行级坐标、
    ``session_id`` 仍是同一个会话：派发出来的子 agent 属于**会话**，不属于主 Agent
    那个 checkpoint thread。

    第四个字段 ``sandbox`` 是**部署策略**（宿主根 + 挂载表），见
    :class:`~evaharness.sandbox.paths.SandboxLayout`。它**有默认值**：不碰文件的图（中间的
    中间件、纯推理的测试）不必构造它，而用了文件工具却没注入布局的运行会**响亮地失败**
    （:func:`~evaharness.runtime.sandbox_layout` 取不到就抛），不是悄悄写进某个猜出来的目录。

    **它标注成 ``Any`` 而不是 ``SandboxLayout``，是被 pydantic 逼的，不是偷懒**：本类型会随
    ``ToolRuntime[RunContext]`` 进工具的参数 schema，而 pydantic 建 schema 时会**真的解析**
    字段标注。写成 ``SandboxLayout | None`` 就得让本模块在运行期 import ``evaharness.sandbox``，
    那正好成环（``sandbox`` 的 ``__init__`` 拉进 bash / file_ops，它们反过来 import 本包）。
    实测症状是一条 ``PydanticUserError: `task` is not fully defined; you should define
    `SandboxLayout```——**工具在第一次被调用时炸**，不是启动时。

    语义上 ``Any`` 也诚实：运行层从不碰这个对象的方法，只是把它原样带着走；真正认它的是
    ``evaharness.sandbox``，而访问器 :func:`~evaharness.runtime.sandbox_layout` 对外的返回
    类型仍是 ``SandboxLayout``（函数的标注不会被 pydantic 解析）。
    """

    session_id: str
    thread_id: str
    run_id: str
    sandbox: Any = None


__all__ = ["RunContext"]
