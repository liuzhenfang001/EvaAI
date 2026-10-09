"""文件沙箱：模型只看得见**虚拟路径**，宿主路径不外泄。

单用户部署，所以只做**会话级**隔离（DeerFlow 还多一层用户隔离，这里不需要）。目录布局：

===================  ==========================================  ========
虚拟路径              宿主位置                                      权限
===================  ==========================================  ========
``/workspace``       ``.evaai/user/{会话}/workspace``（相对路径的落点）  读写
``/output``          ``.evaai/user/{会话}/output``                  读写
``/input``           ``.evaai/user/{会话}/input``                   **只读**
``/skills``          ``.evaai/skills-view``（全局，不按会话分）        **只读**
===================  ==========================================  ========

**为什么 input / skills 只读**：前者是用户上传的原件，改坏了不可逆——要改就复制到
workspace 改；后者是 agent 的行为指南，能被自己改写的技能等于把提示注入面交给模型。
代价只是模型多一次复制。

**为什么不把宿主路径给模型**：①信息泄漏（暴露本机目录布局）；②不可移植（换台机器
就不存在，而对话里学到的路径会留在上下文里）；③``OSError`` 的消息天然带宿主路径。
所以本模块的异常**自己**用虚拟路径措辞，而不是把原始 ``OSError`` 往上抛。

将来接真沙箱时，换的是这一层「挂载表怎么渲染成宿主路径」，工具签名不变。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

SANDBOX_ROOT = Path(__file__).resolve().parents[2] / ".evaai"
"""宿主根（``backend/.evaai``）。

``parents[2]``：``app/tools/sandbox.py`` → ``tools`` → ``app`` → ``backend``。用 ``__file__``
上溯而不是 cwd，与 ``app.py`` 锚 ``PROJECT_ROOT`` 同一手法。

**必须在调用时读这个模块全局，不能写成默认参数值**——默认参数在 import 时就求值固定，
测试 monkeypatch 它就再也不生效了。
"""

_SESSION = "{session}"

_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
"""会话 id 会当路径段用，所以必须在进来之前就挡住 ``..`` / ``a/b`` / 空串。"""

_DRIVE = re.compile(r"^[A-Za-z]:")


class PathNotAllowedError(PermissionError):
    """虚拟路径越界、只读、或格式非法。

    继承 ``PermissionError``：调用方按语义捕获「不许」这类失败时，不必知道我们的类名。
    """


@dataclass(frozen=True, slots=True)
class Mount:
    """一个挂载点：虚拟前缀 ↔ 宿主目录。"""

    virtual: str
    writable: bool
    parts: tuple[str, ...]

    def host_root(self, root: Path, session: str) -> Path:
        """算出这个挂载点的宿主目录（``{session}`` 占位符在这里被替换）。"""
        return root.joinpath(*(session if part == _SESSION else part for part in self.parts))


MOUNTS: tuple[Mount, ...] = (
    Mount("/input", writable=False, parts=("user", _SESSION, "input")),
    Mount("/output", writable=True, parts=("user", _SESSION, "output")),
    Mount("/workspace", writable=True, parts=("user", _SESSION, "workspace")),
    Mount("/skills", writable=False, parts=("skills-view",)),
)

DEFAULT_MOUNT = "/workspace"
"""相对路径的落点：工作台就是「随手写点东西」的地方，模型不必每次写全路径。"""

SESSION_MOUNTS = tuple(mount for mount in MOUNTS if _SESSION in mount.parts)
"""按会话分的挂载点（``/input`` ``/output`` ``/workspace``）。

``/skills`` 不在其中：它是**全局共享**的，不随会话建。它的生产者是
:func:`app.gateway.skills.project_skills`（T14 起）——**启动期**把仓库里的技能源目录镜像过来，
所以它既不按会话分，也不由谁在会话里创建。
"""

_MOUNT_LIST = "、".join(mount.virtual for mount in MOUNTS)


def _normalize(virtual: str) -> str:
    """把模型给的路径归一成 ``/挂载点/...`` 形式，顺手挡掉一切非虚拟写法。"""
    raw = str(virtual or "").strip()
    if not raw:
        return DEFAULT_MOUNT
    # 先归一分隔符：模型可能给 `a\\b`，而 `..\\..` 这种混合写法会漏过逐段检查。
    raw = raw.replace("\\", "/")

    if "\x00" in raw:
        raise PathNotAllowedError(f"路径含非法字符，只支持这些虚拟根：{_MOUNT_LIST}")
    if raw.startswith("~"):
        raise PathNotAllowedError(f"不支持 `~` 开头的路径，只支持这些虚拟根：{_MOUNT_LIST}")
    if raw.startswith("//"):
        raise PathNotAllowedError(f"不支持网络路径，只支持这些虚拟根：{_MOUNT_LIST}")
    if _DRIVE.match(raw):
        raise PathNotAllowedError(f"不支持本机绝对路径（如 C:/...），只支持这些虚拟根：{_MOUNT_LIST}")

    if not raw.startswith("/"):
        raw = f"{DEFAULT_MOUNT}/{raw}"

    segments: list[str] = []
    for segment in raw.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            # 只拦整段等于 `..` 的：`a..b.txt` 是合法文件名。
            raise PathNotAllowedError(f"路径里不允许出现 `..`，只支持这些虚拟根：{_MOUNT_LIST}")
        segments.append(segment)
    return "/" + "/".join(segments)


def _against_mounts(normalized: str) -> tuple[Mount, str]:
    """判断这个虚拟路径属于哪个挂载点，返回 (挂载点, 挂载点内的相对路径)。"""
    for mount in MOUNTS:
        if normalized == mount.virtual:
            return mount, ""
        if normalized.startswith(f"{mount.virtual}/"):
            return mount, normalized[len(mount.virtual) + 1 :]
    raise PathNotAllowedError(f"{normalized!r} 不在允许的虚拟根内，只支持：{_MOUNT_LIST}")


def _is_within(candidate: Path, root: Path) -> bool:
    """``candidate`` 是否在 ``root`` 之内。

    比较前做 ``normcase``：Windows 上 ``relative_to`` 是大小写敏感的字符串比较，盘符与
    目录名大小写来源不一致时会把**合法**路径判成越权（假阳性在安全侧，但会变成
    「明明写不进去」这种很怪的 bug）。
    """
    left, right = os.path.normcase(str(candidate)), os.path.normcase(str(root))
    return left == right or left.startswith(right.rstrip(os.sep) + os.sep)


class Sandbox:
    """某个会话的文件沙箱。**每次工具调用现建**——不按会话缓存，那就是无界字典。

    构造时会把这个会话的 ``input`` / ``output`` / ``workspace`` 建出来（幂等）。这不是
    「顺手 mkdir」，而是**修一个真实的行为缺陷**：不建的话，新会话的第一次 ``ls`` 得到的是
    「不存在」，模型据此会判断工作台不可用——而它其实只是空的。工具的返回值是模型唯一能
    看到的世界，所以「空」与「不存在」必须分得开。
    """

    def __init__(self, session_id: str, *, root: Path | None = None) -> None:
        if not _SAFE_SESSION_ID.match(str(session_id or "")):
            raise PathNotAllowedError(f"非法的会话 id：{session_id!r}")
        self.session_id = str(session_id)
        # 调用时读模块全局（理由见 SANDBOX_ROOT 的注释），并**在这里就规范成真实绝对路径**。
        # 不规范化的话，后续每次包含性比较都在拿「解析过的子路径」比「没解析过的根」——
        # Windows 上 `%TEMP%` 常常带 8.3 短名（`ADMINI~1` vs `Administrator`），于是所有
        # 挂载点都判成「不含」，虚拟路径渲染会静默退化成宿主路径。
        self._root = Path(root) if root is not None else SANDBOX_ROOT
        self._root = self._root.resolve()
        self._ensure_session_dirs()

    @classmethod
    def exists(cls, session_id: str, *, root: Path | None = None) -> bool:
        """这个会话的文件空间建过没有——**不产生任何副作用**。

        下载路由靠它挡掉「不存在的会话」：直接构造 :class:`Sandbox` 会顺手把三个目录建出来
        （见 ``__init__`` 的说明），而一次 404 的请求不该在磁盘上留下痕迹。

        判据是「**任意**一个挂载目录存在」而不是「全都存在」：三个目录是
        :meth:`_ensure_session_dirs` 一起建的，实际不会半有半无；真出现半有半无时，
        「任一存在就算存在」的失败模式只是多建两个空目录，而「全都在才算」会对着一个
        完好无损的 ``/output`` 回 404 —— 后者是更难查的错。

        宿主布局的知识继续只留在本模块：调用方不必知道 ``user/{会话}/output`` 这个形状。
        """
        if not _SAFE_SESSION_ID.match(str(session_id or "")):
            return False
        base = (Path(root) if root is not None else SANDBOX_ROOT).resolve()
        return any(mount.host_root(base, str(session_id)).exists() for mount in SESSION_MOUNTS)

    def _ensure_session_dirs(self) -> None:
        """把会话的三个目录建出来（幂等，与 journal store 的按需建目录同一做法）。"""
        for mount in SESSION_MOUNTS:
            try:
                mount.host_root(self._root, self.session_id).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise PathNotAllowedError(
                    f"无法准备会话目录 {mount.virtual}：{exc.strerror or type(exc).__name__}"
                ) from None

    def mount_of(self, virtual: str) -> tuple[Mount, str]:
        """这个虚拟路径落在哪个挂载点的哪个相对位置上。"""
        return _against_mounts(_normalize(virtual))

    def resolve(self, virtual: str, *, write: bool = False) -> Path:
        """把虚拟路径翻成宿主路径；不允许就抛 :class:`PathNotAllowedError`。"""
        mount, relative = self.mount_of(virtual)
        if write and not mount.writable:
            raise PathNotAllowedError(f"{mount.virtual} 是只读的；要写就写到 /workspace 或 /output")

        host_root = mount.host_root(self._root, self.session_id)
        return self._beneath(host_root, relative)

    def contains(self, host: Path) -> bool:
        """这个宿主路径是否落在某个挂载点内。

        ``glob`` / ``rglob`` 的结果必须**逐条**过这一关：``**`` 对符号链接的处理在不同
        Python 版本上不一样，不能指望它自己保证结果没跑出去。
        """
        return any(_is_within(host, mount.host_root(self._root, self.session_id)) for mount in MOUNTS)

    def display(self, host: Path) -> str:
        """宿主路径 → 虚拟路径（工具渲染结果时用这个，别手拼）。"""
        for mount in MOUNTS:
            host_root = mount.host_root(self._root, self.session_id)
            if _is_within(host, host_root):
                relative = os.path.relpath(host, host_root).replace(os.sep, "/")
                return mount.virtual if relative == "." else f"{mount.virtual}/{relative}"
        return self.mask(str(host))

    def mask(self, text: str) -> str:
        """把文本里的宿主根抹掉——输出与漏网异常的最后一道网。"""
        for form in {str(self._root), str(self._root).replace("\\", "/")}:
            text = text.replace(form, "<evaai>")
        return text

    def _beneath(self, host_root: Path, relative: str) -> Path:
        """拼出宿主路径并确认它真的在挂载根内。

        词法层（:func:`_normalize`）看不出符号链接与 Windows junction 逃逸——建 junction
        在 Windows 上甚至不需要管理员权限——所以解析**之后**必须再比一次。
        """
        try:
            resolved = (host_root / relative).resolve() if relative else host_root.resolve()
            boundary = host_root.resolve()
        except OSError as exc:
            # resolve() 对畸形路径会抛，而 str(exc) 自带宿主路径——只取 strerror。
            raise PathNotAllowedError(f"路径无法解析：{exc.strerror or type(exc).__name__}") from None

        if not _is_within(resolved, boundary):
            raise PathNotAllowedError("路径越出了允许的目录（符号链接或相对跳转）")
        return resolved


def skills_view_root(*, root: Path | None = None) -> Path:
    """``/skills`` 挂载点的**宿主**目录。

    做成函数而不是让调用方拼 ``SANDBOX_ROOT / "skills-view"``：宿主布局的知识只留在本模块，而且它
    必须与 :data:`MOUNTS` 里那条 ``Mount("/skills", parts=("skills-view",))`` 始终一致——两处各写
    一份迟早会漂移。

    **调用时读 ``SANDBOX_ROOT`` 全局**（理由同 :data:`SANDBOX_ROOT` 的注释）：测试 monkeypatch 它
    之后这个函数也要跟着变。
    """
    return (Path(root) if root is not None else SANDBOX_ROOT) / "skills-view"


def ensure_session_dirs(session_id: str) -> None:
    """把一个会话的文件空间建出来（幂等）。

    在**会话的第一条消息**上调用，而不是等第一次文件操作：用户要能在对话开始前把文件放进
    ``/input``，而且「这个会话有没有文件空间」不该取决于模型想没想起来调工具。

    实现就是建一个 :class:`Sandbox`——目录创建只有那一处，免得两处各写一份。
    """
    Sandbox(session_id)


__all__ = [
    "DEFAULT_MOUNT",
    "MOUNTS",
    "SANDBOX_ROOT",
    "SESSION_MOUNTS",
    "Mount",
    "PathNotAllowedError",
    "Sandbox",
    "ensure_session_dirs",
    "skills_view_root",
]
