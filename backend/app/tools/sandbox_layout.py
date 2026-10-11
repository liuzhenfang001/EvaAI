"""本部署的沙箱布局：**根在哪儿**，以及用哪张挂载表。

机制（虚拟路径归一化、两层校验、容器）都在 :mod:`evaharness.sandbox` 里；这一层只回答
「这个部署把它们落在磁盘的哪里」。所以这里**只有两个东西**：一个宿主根、一个构造函数。

**为什么「`backend/.evaai`」是 app 的事而不是框架的事**：它是这个仓库的目录约定
（`.evaai/` 已 gitignore，与 `data/` 并列），换个部署完全可以换到别处——比如测试把它指到
`tmp_path`、将来接真沙箱时可能指到一个卷挂载点。ADR-0012 当年把沙箱放在 app 层的理由是
「工具的实现是业务」；T17 把机制上移之后（见 ADR-0020），留下的**这一处**才是真正的业务：
它决定数据落在哪。

挂载表用框架的默认值（``/workspace``、``/output``、``/input``、``/skills``）。要加挂载点或改
只读策略就在这里传 ``mounts=``——工具侧不需要任何改动，它们从运行上下文拿到的就是这个对象。
"""

from __future__ import annotations

from pathlib import Path

from evaharness.sandbox.paths import SandboxLayout

SANDBOX_ROOT = Path(__file__).resolve().parents[2] / ".evaai"
"""宿主根（``backend/.evaai``）。

``parents[2]``：``app/tools/sandbox_layout.py`` → ``tools`` → ``app`` → ``backend``。用
``__file__`` 上溯而不是 cwd，与 ``app.py`` 锚 ``PROJECT_ROOT`` 同一手法。
"""


def build_layout() -> SandboxLayout:
    """建出这个部署的布局。**启动期调一次**，挂到 ``app.state`` 上再下发。"""
    return SandboxLayout(root=SANDBOX_ROOT)


__all__ = ["SANDBOX_ROOT", "build_layout"]
