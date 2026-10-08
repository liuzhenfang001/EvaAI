"""一次运行的参数：那些「只影响这一次请求」的旋钮。

ADR-0011 把「每请求参数从哪来」明确留给了后续切片，并指出图的形状本身可能依赖请求参数——这就是
那份契约的落点。**给每请求旋钮一个具名的家**，而不是让它们一个个变成新函数形参：后者会让
``AgentFactory.lead()`` 的签名随旋钮数量线性膨胀，而且没有一处能一眼看全「这次运行带了什么」。

**什么该放这里、什么绝不能**：只放**只影响请求体**的东西。``timeout`` / ``http_client`` 这类
参与 httpx 客户端缓存键的参数**不许进来**——每请求变会让连接池按请求泛滥（ADR-0011 的硬约束，
``AgentFactory`` 的 docstring 里也写着）。判断标准很简单：**它出现在请求的 JSON 里吗？**
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ThinkingLevel = Literal["off", "low", "high", "max"]
"""思考档位：``off`` 是「极速」，其余三档是思考模式下的力度。

**一档而不是两个字段**（开/关 + 力度）：界面上的选择本来就是四选一（极速 / 思考·low·high·max），
拆成两个字段会多出「关了但还留着力度」这种没有意义的状态。翻成 API 时再拆回
``extra_body.thinking.type`` + ``reasoning_effort`` 两个键。

取值集合与 DeepSeek 认的力度**完全一致**（实测 2026-10-08：``reasoning_effort`` 收
low/high/max）。刻意不照抄 SDK 那套更宽的 ``none/minimal/medium/xhigh``——传一个 provider 不认的
值只会换来一个 400。
"""


@dataclass(frozen=True, slots=True)
class RunOptions:
    """一次运行的参数。缺省值 = 「与不带参数时完全一致」的行为。"""

    thinking: ThinkingLevel = "off"
    """思考模式与深度。

    缺省 ``off`` 是**兼容性的那一侧**：不带参数的老调用方（封存的 Plan-Exec 路由、所有测试）
    行为一个字不变。真正面向用户的缺省（``high``）在请求层给（``ChatRequest``），不在这里——
    这一层只负责「怎么执行」，不负责「用户想要什么」。
    """


__all__ = ["RunOptions", "ThinkingLevel"]
