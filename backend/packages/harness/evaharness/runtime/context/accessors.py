"""从运行上下文里取「这一轮属于哪里」——两种上下文形状只在这里认一次。

运行的上下文有两种形状：

- **dict**：Plan-Exec 的任务执行单元传 ``{"thread_id": <坐标>}``（早期形状）；
- **对象**：主 agent 与子 agent 传 :class:`~evaharness.runtime.RunContext`——它们要
  ``session_id`` / ``run_id`` 供 ``task`` 工具派发，所以用带类型的上下文。

两个消费者：

- 中间件按**检查点坐标**分桶（循环检测的历史、兜底重试的预算）→ :func:`thread_bucket`；
- 文件工具按**会话**决定目录 → :func:`session_id`。这两者不是一回事，
  见 :func:`session_id` 的说明。

只认其中一种的后果是**静默的**：分桶取不到就落进 ``"default"``，于是所有会话共用一本账
——两个并发会话互相累加，而子 agent 的 ``before_agent`` 还会顺手把主 Agent 正在累积的
历史清掉（T8 实测过）。放在这里而不是各处各写一份：原先两处各有一份**一模一样的**
实现，改一处忘一处就成了「一个中间件按坐标分桶、另一个按 default 分桶」这种最难查的
不一致。

.. note::
   原住在 ``agents/middlewares/context.py``（T19 搬来 ``runtime/context/``，见 ADR-0024）。
   它叫「middlewares」是历史——真正的消费者大半不是中间件，而是**工具**
   （``sandbox`` / ``knowledge`` / ``present_files``）。名字把工具引向了 ``agents`` 包，
   于是「写一个工具就得认识 agent 实现」，这正是本片要断掉的那条边。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

# 注意与 `langgraph.runtime.Runtime` 同名不同物：那是上游交给中间件/工具的运行期对象，
# 本包是 EvaAI 自己的「一次运行」契约。绝对 import 下两者不冲突（DeerFlow 同样如此），
# 但读到 `Runtime` 时先看清是从哪来的。
from langgraph.runtime import Runtime

if TYPE_CHECKING:
    # **只在类型检查时导入**，运行期不要：`SandboxLayout` 住的那个包（`evaharness.sandbox`）
    # 会在自己的 `__init__` 里拉进 bash / file_ops 工具，而它们反过来要 import 本模块取
    # `sandbox_layout`——运行期导入就成环。访问器本身只需要把 context 里那个值原样取出来，
    # 不碰这个类，所以把导入关进 TYPE_CHECKING 就能断开（标注反正已经是字符串了）。
    from evaharness.sandbox.paths import SandboxLayout

FALLBACK_BUCKET = "default"
"""取不到坐标时的兜底桶。

它意味着「所有取不到坐标的运行共用一本账」，所以**只在真的没有上下文时**才该出现
（例如不带 context 直接跑图的单元测试）。正常路径上每个 run 都有自己的坐标。
"""

SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
"""会话 id 的形状。

与 ``journal.stores`` 校验 thread_id 用的是同一条规则，理由也一样：**它会当路径段用**
（journal 目录名、文件沙箱的会话目录名）。这里显式重写一遍而不是 import 那个私有正则，
是因为两个模块之间没有依赖关系，为一个正则牵一条 import 不划算；规则本身简单到不会漂。
"""


def thread_bucket(runtime: Runtime) -> str:
    """取这一轮的分桶键（检查点坐标），认 dict 与带 ``thread_id`` 属性的对象两种形状。"""
    context = getattr(runtime, "context", None)
    if context is None:
        return FALLBACK_BUCKET
    value = context.get("thread_id") if isinstance(context, Mapping) else getattr(context, "thread_id", None)
    return str(value) if value else FALLBACK_BUCKET


def session_id(runtime: Runtime | Any) -> str:
    """取这一轮所属的**会话** id（``th-1``），而不是检查点坐标（``th-1:lead``）。

    两者必须分清：坐标是「哪张图的状态」，会话是「用户眼里的一段对话」。文件沙箱按后者
    分目录——按坐标分的话，同一个会话的主 Agent 与子 agent 会各写一个目录。

    取法分两种形状：

    - 对象：直接读 ``session_id`` 字段（权威，不去切它的 ``thread_id``）；
    - dict（Plan-Exec 的执行单元）：坐标按契约**一定**以 ``{会话}:`` 开头，取第一段。
      这条切法复用的是一个既有契约而不是猜——``delete_session_checkpoints`` 的前缀扫描
      依赖同一个事实。可靠的前提是会话 id 不可能含 ``:``（见 :data:`SAFE_SESSION_ID`）。

    **取不到就抛**，不像 :data:`FALLBACK_BUCKET` 那样兜底：分桶兜错只是账本混在一起，
    路径兜错会把两个会话的文件写进同一个目录，而且不可逆。
    """
    context = getattr(runtime, "context", None)
    if context is None:
        raise ValueError("运行上下文缺失，无法确定会话 id")

    if isinstance(context, Mapping):
        coordinate = context.get("thread_id")
        value = str(coordinate).split(":", 1)[0] if coordinate else None
    else:
        value = getattr(context, "session_id", None)

    if not value or not SAFE_SESSION_ID.match(str(value)):
        raise ValueError(f"无法从运行上下文确定会话 id（拿到 {context!r}）")
    return str(value)


def sandbox_layout(runtime: Runtime | Any) -> SandboxLayout:
    """取这一轮的**沙箱布局**（宿主根 + 挂载表 + 相对路径的落点）。

    与 :func:`session_id` 并排、同样的两种形状：对象读 ``sandbox`` 字段，dict 读 ``sandbox`` 键。

    **为什么走上下文而不是全局**：宿主根与挂载表是**部署策略**（本地开发一个根、测试一个
    tmp_path、将来接真沙箱可能是另一套挂载表）。放模块全局就等于框架替部署决定了这件事，
    而且测试只能靠 monkeypatch 那个全局——参照项目正是这么做的（进程级 provider 单例），
    本项目按 ADR-0012 §2 的立场拒绝那条路。

    **取不到就抛**（与 :func:`session_id` 同口径）：工具拿不到布局就没法把虚拟路径翻成宿主
    路径，兜底只会把文件写进某个猜出来的目录——而那是不可逆的。
    """
    context = getattr(runtime, "context", None)
    if context is None:
        raise ValueError("运行上下文缺失，无法确定沙箱布局")

    value = context.get("sandbox") if isinstance(context, Mapping) else getattr(context, "sandbox", None)
    if value is None:
        raise ValueError(f"运行上下文里没有沙箱布局（拿到 {context!r}）")
    return value


__all__ = ["FALLBACK_BUCKET", "SAFE_SESSION_ID", "sandbox_layout", "session_id", "thread_bucket"]
