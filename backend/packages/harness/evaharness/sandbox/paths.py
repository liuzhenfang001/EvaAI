"""虚拟路径沙箱：模型只看得见**虚拟路径**，宿主路径不外泄。

单用户部署，所以只做**会话级**隔离（参照项目还多一层用户隔离，这里不需要）。目录布局：

===================  ==========================================  ========
虚拟路径              宿主位置                                      权限
===================  ==========================================  ========
``/workspace``       ``{根}/user/{会话}/workspace``（相对路径的落点）  读写
``/output``          ``{根}/user/{会话}/output``                  读写
``/input``           ``{根}/user/{会话}/input``                   **只读**
``/skills``          ``{根}/skills-view``（全局，不按会话分）        **只读**
===================  ==========================================  ========

**根与这张表由谁定**：这一层只提供**机制**——`:class:`SandboxLayout` 携带「根 + 挂载表 +
相对路径的落点」，由**调用方**构造并注入。所以框架里没有任何模块全局决定宿主布局（T17 之前
有一个 `SANDBOX_ROOT`，那是这块最别扭的地方）。上面那张表是 :data:`DEFAULT_MOUNTS` 的默认值，
用默认表的部署只需要给一个根。

**为什么 input / skills 只读**：前者是用户上传的原件，改坏了不可逆——要改就复制到
workspace 改；后者是 agent 的行为指南，能被自己改写的技能等于把提示注入面交给模型。
代价只是模型多一次复制。

**为什么不把宿主路径给模型**：①信息泄漏（暴露本机目录布局）；②不可移植（换台机器
就不存在，而对话里学到的路径会留在上下文里）；③``OSError`` 的消息天然带宿主路径。
所以本模块的异常**自己**用虚拟路径措辞，而不是把原始 ``OSError`` 往上抛。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

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
    """一个挂载点：虚拟前缀 ↔ 宿主目录（``{session}`` 占位符在 ``host_root`` 里替换）。"""

    virtual: str
    writable: bool
    parts: tuple[str, ...]

    def host_root(self, root: Path, session: str) -> Path:
        """算出这个挂载点的宿主目录（``{session}`` 占位符在这里被替换）。"""
        return root.joinpath(*(session if part == _SESSION else part for part in self.parts))


DEFAULT_MOUNTS: tuple[Mount, ...] = (
    Mount("/input", writable=False, parts=("user", _SESSION, "input")),
    Mount("/output", writable=True, parts=("user", _SESSION, "output")),
    Mount("/workspace", writable=True, parts=("user", _SESSION, "workspace")),
    Mount("/skills", writable=False, parts=("skills-view",)),
)
"""默认挂载表。用默认表就只需要给一个根，想要别的形状（更多挂载点、不同的只读策略）
就自己传一份——**机制是通用的，表是策略**。"""

DEFAULT_MOUNT = "/workspace"
"""相对路径的落点：工作台就是「随手写点东西」的地方，模型不必每次写全路径。"""


def _is_within(candidate: Path, root: Path) -> bool:
    """``candidate`` 是否在 ``root`` 之内。

    比较前做 ``normcase``：Windows 上 ``relative_to`` 是大小写敏感的字符串比较，盘符与
    目录名大小写来源不一致时会把**合法**路径判成越权（假阳性在安全侧，但会变成
    「明明写不进去」这种很怪的 bug）。
    """
    left, right = os.path.normcase(str(candidate)), os.path.normcase(str(root))
    return left == right or left.startswith(right.rstrip(os.sep) + os.sep)


@dataclass(frozen=True, slots=True)
class SandboxLayout:
    """一次运行要用的沙箱布局：**根 + 挂载表 + 相对路径的落点**。

    这是「机制」与「策略」之间那道缝：本模块提供虚拟路径与两层校验，具体把哪些目录挂到
    哪些虚拟路径、根在磁盘的哪里，由构造它的人（app 层）决定，经运行上下文注入到工具手里。

    **构造时就把根 ``resolve()`` 一次**，理由不是洁癖：不规范化的话，后续每次包含性比较都在
    拿「解析过的子路径」比「没解析过的根」——Windows 的 ``%TEMP%`` 常带 8.3 短名
    （``ADMINI~1`` vs ``Administrator``），于是所有挂载点都判成「不含」，**虚拟路径渲染会静默
    退化成宿主路径**。这个 bug 当年是端到端探针抓到的，单元测试当时全绿。
    """

    root: Path
    mounts: tuple[Mount, ...] = DEFAULT_MOUNTS
    default_mount: str = DEFAULT_MOUNT

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).resolve())

    @property
    def session_mounts(self) -> tuple[Mount, ...]:
        """按会话分的挂载点（``/input`` ``/output`` ``/workspace``）。

        ``/skills`` 不在其中：它是**全局共享**的，不随会话建。它的生产者是技能投影
        （**启动期**把仓库里的技能源目录镜像过来），所以它既不按会话分，也不由谁在会话里创建。
        """
        return tuple(mount for mount in self.mounts if _SESSION in mount.parts)

    @property
    def mount_list_text(self) -> str:
        """错误文案里那句「只支持这些虚拟根」——**从当前挂载表算**，不是写死的常量。"""
        return "、".join(mount.virtual for mount in self.mounts)

    def normalize(self, virtual: str) -> str:
        """把模型给的路径归一成 ``/挂载点/...`` 形式，顺手挡掉一切非虚拟写法。"""
        raw = str(virtual or "").strip()
        if not raw:
            return self.default_mount
        # 先归一分隔符：模型可能给 `a\\b`，而 `..\\..` 这种混合写法会漏过逐段检查。
        raw = raw.replace("\\", "/")
        roots = self.mount_list_text

        if "\x00" in raw:
            raise PathNotAllowedError(f"路径含非法字符，只支持这些虚拟根：{roots}")
        if raw.startswith("~"):
            raise PathNotAllowedError(f"不支持 `~` 开头的路径，只支持这些虚拟根：{roots}")
        if raw.startswith("//"):
            raise PathNotAllowedError(f"不支持网络路径，只支持这些虚拟根：{roots}")
        if _DRIVE.match(raw):
            raise PathNotAllowedError(f"不支持本机绝对路径（如 C:/...），只支持这些虚拟根：{roots}")

        if not raw.startswith("/"):
            raw = f"{self.default_mount}/{raw}"

        segments: list[str] = []
        for segment in raw.split("/"):
            if segment in ("", "."):
                continue
            if segment == "..":
                # 只拦整段等于 `..` 的：`a..b.txt` 是合法文件名。
                raise PathNotAllowedError(f"路径里不允许出现 `..`，只支持这些虚拟根：{roots}")
            segments.append(segment)
        return "/" + "/".join(segments)

    def mount_of(self, virtual: str) -> tuple[Mount, str]:
        """判断这个虚拟路径属于哪个挂载点，返回 (挂载点, 挂载点内的相对路径)。"""
        normalized = self.normalize(virtual)
        for mount in self.mounts:
            if normalized == mount.virtual:
                return mount, ""
            if normalized.startswith(f"{mount.virtual}/"):
                return mount, normalized[len(mount.virtual) + 1 :]
        raise PathNotAllowedError(f"{normalized!r} 不在允许的虚拟根内，只支持：{self.mount_list_text}")

    def session(self, session_id: str) -> Sandbox:
        """这个会话的沙箱。**每次工具调用现建**——不按会话缓存，那就是无界字典。"""
        return Sandbox(self, session_id)

    def exists(self, session_id: str) -> bool:
        """这个会话的文件空间建过没有——**不产生任何副作用**。

        下载路由靠它挡掉「不存在的会话」：直接构造 :class:`Sandbox` 会顺手把三个目录建出来，
        而一次 404 的请求不该在磁盘上留下痕迹。

        判据是「**任意**一个挂载目录存在」而不是「全都存在」：三个目录是一起建的，实际不会
        半有半无；真出现半有半无时，「任一存在就算存在」的失败模式只是多建两个空目录，
        而「全都在才算」会对着一个完好无损的 ``/output`` 回 404 —— 后者是更难查的错。
        """
        if not _SAFE_SESSION_ID.match(str(session_id or "")):
            return False
        return any(mount.host_root(self.root, str(session_id)).exists() for mount in self.session_mounts)

    def ensure_session_dirs(self, session_id: str) -> None:
        """把一个会话的文件空间建出来（幂等）——实现就是建一个 :class:`Sandbox`。"""
        self.session(session_id)

    def skills_view_root(self) -> Path:
        """``/skills`` 挂载点的**宿主**目录。

        从**当前挂载表**解析而不是让调用方拼 ``根 / "skills-view"``：宿主布局的知识只留在本模块，
        而且它必须与挂载表里那条 ``/skills`` 始终一致——两处各写一份迟早会漂移。
        """
        for mount in self.mounts:
            if mount.virtual == "/skills":
                return mount.host_root(self.root, "")
        raise PathNotAllowedError("当前挂载表里没有 /skills，技能视图无处安放")

    def host_forms(self) -> set[str]:
        """宿主根的各种写法——掩码要盖住 docker / OSError 可能吐出来的每一样。"""
        forms = {str(self.root)}
        forms |= {form.replace("\\", "/") for form in tuple(forms)}
        forms |= {form[0].lower() + form[1:] for form in tuple(forms) if len(form) > 1 and form[1] == ":"}
        forms |= {f"\\\\?\\{form}" for form in tuple(forms) if "\\" in form}
        # 太短的串（比如根目录）替换起来会误伤正文。
        return {form for form in forms if len(form) > 4}

    def mask(self, text: str) -> str:
        """把宿主根从文本里抹掉——输出与漏网异常的**最后一道网**。

        比只替换 ``str(root)`` 宽：docker 的报错会吐 ``\\\\?\\D:\\…``、``D:/…``（正斜杠）、
        小写盘符这类变体。ADR-0012 记过一次被路径写法咬到的实测（``%TEMP%`` 的 8.3 短名
        ``ADMINI~1``），所以这里宁宽勿窄。
        """
        for form in self.host_forms():
            text = text.replace(form, "<evaai>")
        return text


class Sandbox:
    """某个会话在这个布局下的文件沙箱。

    构造时会把这个会话的 ``input`` / ``output`` / ``workspace`` 建出来（幂等）。这不是
    「顺手 mkdir」，而是**修一个真实的行为缺陷**：不建的话，新会话的第一次 ``ls`` 得到的是
    「不存在」，模型据此会判断工作台不可用——而它其实只是空的。工具的返回值是模型唯一能
    看到的世界，所以「空」与「不存在」必须分得开。
    """

    def __init__(self, layout: SandboxLayout, session_id: str) -> None:
        if not _SAFE_SESSION_ID.match(str(session_id or "")):
            raise PathNotAllowedError(f"非法的会话 id：{session_id!r}")
        self.layout = layout
        self.session_id = str(session_id)
        self._ensure_session_dirs()

    def _ensure_session_dirs(self) -> None:
        """把会话的三个目录建出来（幂等，与 journal store 的按需建目录同一做法）。"""
        for mount in self.layout.session_mounts:
            try:
                mount.host_root(self.layout.root, self.session_id).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise PathNotAllowedError(
                    f"无法准备会话目录 {mount.virtual}：{exc.strerror or type(exc).__name__}"
                ) from None

    def mount_of(self, virtual: str) -> tuple[Mount, str]:
        """这个虚拟路径落在哪个挂载点的哪个相对位置上。"""
        return self.layout.mount_of(virtual)

    def resolve(self, virtual: str, *, write: bool = False) -> Path:
        """把虚拟路径翻成宿主路径；不允许就抛 :class:`PathNotAllowedError`。"""
        mount, relative = self.mount_of(virtual)
        if write and not mount.writable:
            raise PathNotAllowedError(f"{mount.virtual} 是只读的；要写就写到 /workspace 或 /output")

        host_root = mount.host_root(self.layout.root, self.session_id)
        return self._beneath(host_root, relative)

    def contains(self, host: Path) -> bool:
        """这个宿主路径是否落在某个挂载点内。

        ``glob`` / ``rglob`` 的结果必须**逐条**过这一关：``**`` 对符号链接的处理在不同
        Python 版本上不一样，不能指望它自己保证结果没跑出去。
        """
        return any(
            _is_within(host, mount.host_root(self.layout.root, self.session_id)) for mount in self.layout.mounts
        )

    def display(self, host: Path) -> str:
        """宿主路径 → 虚拟路径（工具渲染结果时用这个，别手拼）。"""
        for mount in self.layout.mounts:
            host_root = mount.host_root(self.layout.root, self.session_id)
            if _is_within(host, host_root):
                relative = os.path.relpath(host, host_root).replace(os.sep, "/")
                return mount.virtual if relative == "." else f"{mount.virtual}/{relative}"
        return self.mask(str(host))

    def mask(self, text: str) -> str:
        """把文本里的宿主根抹掉——见 :meth:`SandboxLayout.mask`。"""
        return self.layout.mask(text)

    def _beneath(self, host_root: Path, relative: str) -> Path:
        """拼出宿主路径并确认它真的在挂载根内。

        词法层（:meth:`SandboxLayout.normalize`）看不出符号链接与 Windows junction 逃逸——
        建 junction 在 Windows 上甚至不需要管理员权限——所以解析**之后**必须再比一次。
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


__all__ = [
    "DEFAULT_MOUNT",
    "DEFAULT_MOUNTS",
    "Mount",
    "PathNotAllowedError",
    "Sandbox",
    "SandboxLayout",
]
