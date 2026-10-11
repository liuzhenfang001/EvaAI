"""docker 沙箱：把命令送进**本会话私有**的 Linux 容器里执行。

:mod:`evaharness.sandbox.paths`（软沙箱）管的是「模型的虚拟路径 → 宿主目录」这一段；本模块管的是
「宿主目录 → 容器内路径」那一段。ADR-0012 说的「将来接真沙箱时，换的是这一层『挂载表怎么渲染
成宿主路径』」，换的就是这里——工具签名与虚拟路径的语义都没动。

**布局从外面传进来**，本模块自己不含任何全局：每个入口（`run_command` / `reconcile_orphans` /
`remove_session_container` / `ensure_mount_check`）第一个参数都是
:class:`~evaharness.sandbox.paths.SandboxLayout`。宿主根与挂载表是**部署策略**，由调用方决定
（真沙箱 vs 软沙箱的分界就是这里没有第二个权威）。

**容器内路径就是虚拟路径。** ``Sandbox.resolve()`` 给出某个挂载点的宿主目录，我们把它 bind 到
容器里**同名**路径。于是模型仍然只面对一套坐标系（``/workspace`` 等四个），而文件工具与容器里
跑的命令看到的是**同一份文件**——不是靠约定对齐，是同一个目录。宿主路径永远不进模型上下文，
docker 自己吐的报错也要过 :meth:`SandboxLayout.mask`。

**每会话一个长驻容器**，不是每次调用一个：装过的包在会话里留存。容器名由会话 id 推出
（``evaai-<会话>``），所以**不需要任何内存映射**——每次调用先试 ``exec``，容器不可用才建。
并发首次调用靠 docker 的全局名字唯一性兜底：一个 ``docker run`` 赢，另一个撞名失败后直接复用
（比 ``dict[会话, Lock]`` 简单，也避开了 ADR-0012 警告的那个无界字典）。

**失败形态**：docker 这一层失败（CLI 不在、daemon 连不上、容器建不出来）raise
:class:`DockerError`，由 ``ToolErrorHandlingMiddleware`` 转成 ``status="error"`` 的 ToolMessage——
所以**运行期**的文案是英文、且不含宿主路径（与 :mod:`evaharness.sandbox.file_ops` 同一口径）。
而**命令自己的非零退出码不是失败**，是结果：照常返回、附上退出码（渲染在
:mod:`evaharness.sandbox.bash`）。这是对 ADR-0012 §6「失败一律 raise」的**有意偏离**——那条针对的是
「工具没能完成它的工作」，而 shell 命令的非零退出是**结果**。

`check_docker_ready` 是启动期校验（由建工具的工厂调），文案给运维看，所以是中文。它**不碰
layout**：那一步只需要镜像名——哨兵文件由 app 在 lifespan 里另调 :func:`ensure_mount_check`，
因为启动期校验发生时布局还不存在。

**为什么不改文件工具**：既然容器内路径就是虚拟路径，再写一套「docker 内文件操作」等于造出两个
坐标系、两个权威，哪天某个挂载点在两边漂了，模型会看到两个互相矛盾的世界。ADR-0012 那句
「换的是这一层挂载表怎么渲染」要的就是不要第二套。
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
import sys
import threading
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from evaharness.sandbox.paths import Sandbox, SandboxLayout

logger = logging.getLogger(__name__)

_BASH = "bash"
_WORKDIR = "/workspace"

_CONTAINER_PREFIX = "evaai-"
_LABEL_MANAGED = "evaai.managed"
_LABEL_DEPLOYMENT = "evaai.deployment"
_LABEL_SESSION = "evaai.session"

_MOUNT_CHECK_DIR = ".mount-check"
_MOUNT_CHECK_NAME = "ok"
_MOUNT_CHECK_TARGET = "/evaai-check"

_CLIENT_GRACE_SECONDS = 10
"""客户端超时比容器内 ``timeout`` 多这么多秒。

正常情况下容器内那个先到（退出码 124），客户端的只是兜底——**它杀不掉容器里的进程**
（那条进程归 dockerd 管），所以容器内的 ``timeout`` 才是真边界。
"""

_KILL_GRACE_SECONDS = 5
"""``timeout -k 5``：命令无视 SIGTERM 的话，5 秒后 SIGKILL。不带 ``-k`` 会永久挂住。"""

_READ_CAP_BYTES = 256 * 1024
"""单次调用最多读这么多字节。

按**字节**设上限而不是靠 ``capture_output`` 全缓冲：``yes`` / ``cat /dev/urandom`` 在超时之前
能吐几十 GB，全缓冲就是把服务 OOM 掉。读满就停止读取并杀掉客户端。
"""

_JOIN_SECONDS = 5
_DOCKER_TIMEOUT = 10
"""``docker info`` / ``image inspect`` / ``ps`` 这类短命令的超时——daemon 挂着不能拖死启动。"""

_CREATE_TIMEOUT = 120
"""建容器的超时：镜像已在本地，正常是一两秒的量级。"""

_ALREADY_EXISTS = b"already in use"
_GONE_MARKERS = (b"No such container", b"is not running")
"""``docker exec`` 说容器没了/没跑——要重建。注意**退出码 127 是容器内合法的「命令未找到」**，
所以判据是这几个标记，不是退出码。
"""

_FILE_SHARING_HINT = (
    "The sandbox container started but cannot see this session's files. Docker Desktop is "
    "probably not sharing the drive this project lives on. Open Docker Desktop → Settings → "
    "Resources → File Sharing, add that drive, then retry."
)


@dataclass(frozen=True, slots=True)
class Completed:
    """一次 docker 调用的结果。``output`` 是 stdout 与 stderr **合并**后的原始字节。

    合并（``stderr=STDOUT``）而不是分流：一个线程读两个管道会死锁（要上 selectors 或双线程），
    而终端本来就是合并的。代价是拿不到「哪句是 stderr」，换来的是这段代码没有并发机关。
    """

    returncode: int
    output: bytes
    truncated: bool = False
    """读满了 :data:`_READ_CAP_BYTES`。此时 ``returncode`` 是我们自己杀出来的，别当命令的结果看。"""


class DockerError(RuntimeError):
    """docker 这一层失败——**不是**命令失败。

    命令失败了照常返回（附退出码）；只有「工具没能把命令跑起来」才走到这里。
    """


class Runner(Protocol):
    """跑一次 docker 命令行。

    做成协议是为了在测试里注入假实现——工具层不直接碰 ``subprocess``，于是单测不需要本机有
    docker（与联网工具在 httpx 边界打桩同一手法）。
    """

    def invoke(self, argv: list[str], *, timeout: float) -> Completed: ...


def _read_bounded(stream) -> tuple[bytes, bool]:
    """读到 EOF 或读满上限为止，返回 (内容, 是否截断)。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(65536)
        if not chunk:
            return b"".join(chunks), False
        remaining = _READ_CAP_BYTES - total
        if len(chunk) > remaining:
            # **严格大于**才是「有东西被丢掉」。正好读满时继续循环：下一轮要么读到 EOF
            # （那就没截断），要么读到更多（那时 remaining 已是 0，必然判成截断）。
            chunks.append(chunk[:remaining])
            return b"".join(chunks), True
        chunks.append(chunk)
        total += len(chunk)


class DockerCli:
    """真的去跑 ``docker`` 可执行文件。**同步**，由 bash 工具丢进 worker 线程。

    为什么不用 ``asyncio.create_subprocess_exec``：它在 Windows 上要求 Proactor 事件循环，
    而 uvicorn 为了 Ctrl+C 的历史原因可能跑在 Selector 上，那时直接 ``NotImplementedError``。
    丢进线程用同步 API，事件循环类型就不再是前提。
    """

    def invoke(self, argv: list[str], *, timeout: float) -> Completed:
        # 不加这个的话，Windows 上每跑一次 docker.exe 都会闪一个控制台窗口。
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            # stdin 给 DEVNULL 而不是留着：容器里读 stdin 的命令（cat、交互式工具）会拿到 EOF
            # 而不是挂在那儿等一个永远不来的输入。
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        collected: list[tuple[bytes, bool]] = []

        def drain() -> None:
            output, truncated = _read_bounded(process.stdout)
            if truncated:
                # 读满上限就不再读了；此时进程会阻塞在写满的管道上，必须由我们结束它。
                process.kill()
            collected.append((output, truncated))

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            reader.join(_JOIN_SECONDS)
            raise DockerError(
                "docker stopped responding (the daemon looks stuck). Check that Docker Desktop is running, then retry."
            ) from None
        reader.join(_JOIN_SECONDS)
        output, truncated = collected[0] if collected else (b"", False)
        return Completed(returncode=returncode, output=output, truncated=truncated)


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    """建容器与跑命令的旋钮。全部来自 ``config.yaml`` 的 tools 条目，启动期绑进闭包。"""

    image: str
    network: str = "bridge"
    timeout: int = 60
    memory: str = "1g"
    cpus: str = "2"
    pids_limit: int = 512


# --------------------------------------------------------------------------- #
# 宿主路径与掩码
# --------------------------------------------------------------------------- #


def container_name(session_id: str) -> str:
    """容器名由会话 id 推出——所以不需要任何「会话 → 容器」的内存映射。"""
    return f"{_CONTAINER_PREFIX}{session_id}"


def deployment_id(layout: SandboxLayout) -> str:
    """本部署的稳定标识，**跨重启不变**。

    不用「每次启动生成一个 uuid」：那会让启动期清理把上一轮留下的容器全删掉，也就毁掉了
    「后端重启后仍存活的会话复用原容器」这件事。用宿主根路径的哈希——同一份数据目录就是同一个
    部署，清理只扫自己这一份。
    """
    return hashlib.sha1(str(layout.root).encode("utf-8")).hexdigest()[:12]


def _host_path(path: Path) -> str:
    """宿主路径 → 传给 ``-v`` 的写法。

    Windows 上 ``D:\\a\\b`` 的反斜杠在 ``宿主:容器:ro`` 这种冒号分隔的写法里容易被误读，
    统一转正斜杠（Docker Desktop 两种都收）。整个 spec 是**一个 argv 元素**、全程不走 shell，
    所以既不需要引号，也躲开了 Git Bash 的 MSYS 路径改写。
    """
    return str(path).replace("\\", "/")


def mount_check_dir(layout: SandboxLayout) -> Path:
    """哨兵目录的宿主位置（见 :func:`ensure_mount_check`）。"""
    return layout.root / _MOUNT_CHECK_DIR


def ensure_mount_check(layout: SandboxLayout) -> Path:
    """建出哨兵文件，返回它的目录。

    **为什么需要它**：Docker Desktop 没共享某个盘符时，``-v D:/…`` **不报错**——它会在容器里
    建一个**空目录**。模型看到空的 ``/workspace``，一头雾水，而且 ``docker inspect`` 也看不出
    区别（Mount 条目照样在）。所以挂一个模型看不见的哨兵进去，容器建好后探一次：哨兵不在，
    就是盘符没共享。把最坏的那种静默失败变成响的（与 ADR-0012 记「虚拟路径静默退化」同一取向）。
    """
    directory = mount_check_dir(layout)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / _MOUNT_CHECK_NAME).write_text("evaai\n", encoding="utf-8")
    return directory


def _short(text: str, layout: SandboxLayout, limit: int = 300) -> str:
    """异常文案要短——中间件会截到 500 字符，而且模型只需要读到「为什么」。

    掩码走 :meth:`SandboxLayout.mask`（「宿主根的各种写法」那一整套住在 paths.py）：
    **运行期**的文案是模型看得见的，必须抹掉宿主路径。
    """
    collapsed = " ".join(layout.mask(text).split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 3] + "..."


def _detail(result: Completed, layout: SandboxLayout) -> str:
    return _short(result.output.decode("utf-8", errors="replace"), layout) or f"exit code {result.returncode}"


def _plain_detail(result: Completed) -> str:
    """启动期那几条报错的详情——**不掩码**。

    启动期还没有 layout（校验发生在注册表建工具的那一刻），而这几条 docker 报错说的是
    CLI / daemon / 镜像，不涉及挂载，所以不带宿主路径；文案本身也是给运维看的中文，
    不进模型上下文。
    """
    collapsed = " ".join(result.output.decode("utf-8", errors="replace").split())
    return collapsed[:297] + "..." if len(collapsed) > 300 else collapsed


# --------------------------------------------------------------------------- #
# 启动期校验
# --------------------------------------------------------------------------- #


def check_docker_ready(image: str, *, runner: Runner | None = None) -> None:
    """启动期把「启用 bash」这件事的每个前提验一遍，不合格就拒绝启动。

    顺序是有意的：先「有没有 CLI」→「daemon 通不通」→「镜像在不在」→「镜像里有没有我们要的
    三样东西」。最后那步不能省：镜像里没有 ``bash`` 的话，症状是**模型第一次调用**时才炸，
    而那时离「你刚改了配置」已经很远（ADR-0012 2b 的教训：缺配置的失败模式要按实测写）。

    Raises:
        DockerError: 文案是**中文**——这一条给运维看，与注册表的启动期校验同一口径。
    """
    cli = runner or DockerCli()

    if shutil.which("docker") is None:
        raise DockerError(
            "config.yaml 里启用了 bash 工具，但本机找不到 docker 命令。"
            "装并启动 Docker Desktop 之后重启服务，或者把 `tools:` 里 bash 那一条注释掉。"
        )

    info = cli.invoke(["docker", "info"], timeout=_DOCKER_TIMEOUT)
    if info.returncode != 0:
        raise DockerError(
            "docker daemon 连不上，bash 工具没法用。确认 Docker Desktop 已经启动，然后重启服务。"
            f"（docker info 说：{_plain_detail(info)}）"
        )

    inspected = cli.invoke(["docker", "image", "inspect", image], timeout=_DOCKER_TIMEOUT)
    if inspected.returncode != 0:
        raise DockerError(
            f"镜像 {image!r} 不在本机。先 `docker pull {image}`（不自动拉：它可能是几个 GB），再重启服务。"
        )

    probe = cli.invoke(
        ["docker", "run", "--rm", "--entrypoint", "/bin/sh", image, "-c", f"command -v {_BASH} timeout sleep"],
        timeout=_CREATE_TIMEOUT,
    )
    if probe.returncode != 0:
        raise DockerError(
            f"镜像 {image!r} 里缺 bash / timeout / sleep，不能当沙箱用。换一个带 GNU coreutils 的镜像。"
            f"（探针说：{_plain_detail(probe)}）"
        )


# --------------------------------------------------------------------------- #
# 命令行拼装
# --------------------------------------------------------------------------- #


def _mount_specs(box: Sandbox) -> list[str]:
    """四个挂载点的 ``-v`` 参数：宿主目录来自 :meth:`Sandbox.resolve`，容器内用**虚拟路径**。

    ``/input`` 与 ``/skills`` 带 ``:ro``——只读因此从「工具自己遵守的约定」变成 **docker 强制的
    边界**：容器里的 ``touch /input/x`` 会真的失败，不是我们拦下来的。
    """
    specs: list[str] = []
    for mount in box.layout.mounts:
        host = _host_path(box.resolve(mount.virtual))
        specs += ["-v", f"{host}:{mount.virtual}" + ("" if mount.writable else ":ro")]
    return specs


def _create_argv(box: Sandbox, spec: SandboxSpec) -> list[str]:
    """``docker run -d``：建一个长驻容器，主进程是 ``sleep infinity``。"""
    argv = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        container_name(box.session_id),
        "--label",
        f"{_LABEL_MANAGED}=1",
        "--label",
        f"{_LABEL_DEPLOYMENT}={deployment_id(box.layout)}",
        "--label",
        f"{_LABEL_SESSION}={box.session_id}",
        "--network",
        spec.network,
        "--security-opt",
        "no-new-privileges",
        # 镜像自带的 HEALTHCHECK 探的是它那套服务的端口（8091 / 浏览器 9222），而我们刻意不启动
        # 那套服务——不关掉的话，容器会**永远显示 unhealthy**（实测：`docker ps` 里
        # "Up 6 seconds (health: starting)"），看着像坏了，而且 docker 会一直白跑那个探针。
        "--no-healthcheck",
        "--workdir",
        _WORKDIR,
    ]
    if spec.memory:
        # memory-swap 与 memory 等值 = 不许用 swap：不等值的话容器能靠 swap 吃穿上限。
        argv += ["--memory", spec.memory, "--memory-swap", spec.memory]
    if spec.cpus:
        argv += ["--cpus", str(spec.cpus)]
    if spec.pids_limit:
        argv += ["--pids-limit", str(spec.pids_limit)]

    argv += _mount_specs(box)
    argv += ["-v", f"{_host_path(mount_check_dir(box.layout))}:{_MOUNT_CHECK_TARGET}:ro"]

    # 覆盖镜像自带的 ENTRYPOINT。AIO 沙箱镜像的 entrypoint 是 /opt/gem/run.sh（Jupyter /
    # code-server / VNC / 内嵌浏览器一整套服务），不覆盖的话每个会话都会拉起那一整套。
    # 我们只要一个「文件系统 + 工具链」，命令靠 exec 送进去。
    argv += ["--entrypoint", "sleep", spec.image, "infinity"]
    return argv


def _exec_argv(session_id: str, command: str, spec: SandboxSpec) -> list[str]:
    """``docker exec``：把命令包在容器内的 ``timeout`` 里跑。

    命令是**一个 argv 元素**——模型写的引号、管道、重定向都交给容器里的 bash 解析，我们不经手。
    用 ``bash -c`` 而不是 ``-lc``：镜像的 PATH 已经在 ENV 里，登录 shell 只会多跑一遍
    profile 脚本（噪音 + 启动开销）。
    """
    inner = max(1, int(spec.timeout))
    return [
        "docker",
        "exec",
        "-w",
        _WORKDIR,
        container_name(session_id),
        "timeout",
        "-k",
        str(_KILL_GRACE_SECONDS),
        str(inner),
        _BASH,
        "-c",
        command,
    ]


# --------------------------------------------------------------------------- #
# 跑命令
# --------------------------------------------------------------------------- #


def run_command(
    layout: SandboxLayout,
    session_id: str,
    command: str,
    spec: SandboxSpec,
    *,
    runner: Runner | None = None,
) -> Completed:
    """把 ``command`` 送进这个会话的容器里跑。**同步**，调用方负责丢进 worker 线程。

    Raises:
        DockerError: docker 这一层失败（文案英文，模型看得见）。
    """
    cli = runner or DockerCli()
    # 构造 Sandbox 会顺手把会话的三个目录建出来——`-v` 的来源必须是真目录，否则 docker 会在
    # 宿主上建一个 root 属主的空目录。
    box = layout.session(session_id)
    deadline = spec.timeout + _CLIENT_GRACE_SECONDS

    executed = cli.invoke(_exec_argv(session_id, command, spec), timeout=deadline)
    if not _container_gone(executed):
        return executed

    _start_container(cli, box, spec)
    return cli.invoke(_exec_argv(session_id, command, spec), timeout=deadline)


def _container_gone(result: Completed) -> bool:
    """容器不存在/没在跑——要重建。判据是 docker 的报错标记，**不是退出码**。"""
    return result.returncode != 0 and any(marker in result.output for marker in _GONE_MARKERS)


def _start_container(cli: Runner, box: Sandbox, spec: SandboxSpec) -> None:
    created = cli.invoke(_create_argv(box, spec), timeout=_CREATE_TIMEOUT)
    if created.returncode != 0:
        if _ALREADY_EXISTS in created.output:
            # 并发首次调用：另一个请求刚把这个名字占了，容器是好的，直接用。
            # docker 的全局名字唯一性就是这里的互斥锁。
            return
        raise DockerError(f"could not start the sandbox container: {_detail(created, box.layout)}")

    probe = cli.invoke(
        ["docker", "exec", container_name(box.session_id), "test", "-f", f"{_MOUNT_CHECK_TARGET}/{_MOUNT_CHECK_NAME}"],
        timeout=_DOCKER_TIMEOUT,
    )
    if probe.returncode != 0:
        # 容器起来了但看不到宿主的挂载点——别留个坏容器在 ps 里。
        cli.invoke(["docker", "rm", "-f", container_name(box.session_id)], timeout=_DOCKER_TIMEOUT)
        raise DockerError(_FILE_SHARING_HINT)


# --------------------------------------------------------------------------- #
# 生命周期：启动期清理孤儿 + 删会话时连带删容器
# --------------------------------------------------------------------------- #


def _list_argv(layout: SandboxLayout, session_id: str | None = None, *, with_session_label: bool = False) -> list[str]:
    """列本部署的沙箱容器。

    ``with_session_label`` 时每行是 ``ID<TAB>会话``（清理孤儿要按会话判存活）；否则只回 ID。
    **两种格式不能混用**：只回 ID 的那种若照 tab 格式去按空白切，一行会被切成两个 token。
    """
    argv = [
        "docker",
        "ps",
        "-a",
        "--filter",
        f"label={_LABEL_MANAGED}=1",
        "--filter",
        f"label={_LABEL_DEPLOYMENT}={deployment_id(layout)}",
    ]
    if session_id is not None:
        argv += ["--filter", f"label={_LABEL_SESSION}={session_id}"]
    fmt = '{{.ID}}\t{{.Label "' + _LABEL_SESSION + '"}}' if with_session_label else "{{.ID}}"
    argv += ["--format", fmt]
    return argv


def reconcile_orphans(layout: SandboxLayout, live_sessions: Collection[str], *, runner: Runner | None = None) -> int:
    """启动期清理：删掉「本部署建的、但会话已不在 journal 里」的容器。返回删了几个。

    **判据为什么是 journal 而不是会话目录**：ADR-0012 记着「删会话不删文件」这条债——
    ``DELETE /threads/{id}`` 并不删 ``.evaai/user/{会话}/``，所以「目录不存在」这个判据几乎
    永不命中，会一个都清不掉。

    **永不抛**：清理不该拦着服务起来（与启动期的 checkpoint 清理同一形态），
    失败只记日志。这条由调用方负责吞异常，这里自己也不往上抛。
    """
    cli = runner or DockerCli()
    live = set(live_sessions)
    try:
        listed = cli.invoke(_list_argv(layout, with_session_label=True), timeout=_DOCKER_TIMEOUT)
    except Exception:
        logger.warning("沙箱容器清理失败，跳过", exc_info=True)
        return 0
    if listed.returncode != 0:
        logger.warning("沙箱容器清理失败，跳过：%s", _detail(listed, layout))
        return 0

    removed = 0
    for line in listed.output.decode("utf-8", errors="replace").splitlines():
        container_id, _, session = line.partition("\t")
        if not container_id or session in live:
            continue
        try:
            result = cli.invoke(["docker", "rm", "-f", container_id], timeout=_DOCKER_TIMEOUT)
        except Exception:
            logger.warning("删不掉孤儿沙箱容器 %s，跳过", container_id, exc_info=True)
            continue
        if result.returncode == 0:
            removed += 1
        else:
            logger.warning("删不掉孤儿沙箱容器 %s：%s", container_id, _detail(result, layout))
    return removed


def remove_session_container(layout: SandboxLayout, session_id: str, *, runner: Runner | None = None) -> int:
    """删掉某个会话的容器，返回删了几个（``DELETE /threads/{id}`` 用）。

    按 label 找而不是按容器名：名字是 ``evaai-<会话>``，同机另一个部署撞上同一个会话 id 时会误删。
    best-effort 由调用方决定——这里该失败就抛。
    """
    cli = runner or DockerCli()
    listed = cli.invoke(_list_argv(layout, session_id), timeout=_DOCKER_TIMEOUT)
    if listed.returncode != 0:
        raise DockerError(f"could not list sandbox containers: {_detail(listed, layout)}")

    removed = 0
    for container_id in listed.output.decode("utf-8", errors="replace").split():
        result = cli.invoke(["docker", "rm", "-f", container_id], timeout=_DOCKER_TIMEOUT)
        if result.returncode != 0:
            raise DockerError(f"could not remove sandbox container {container_id}: {_detail(result, layout)}")
        removed += 1
    return removed


__all__ = [
    "Completed",
    "DockerCli",
    "DockerError",
    "Runner",
    "SandboxSpec",
    "check_docker_ready",
    "container_name",
    "deployment_id",
    "ensure_mount_check",
    "mount_check_dir",
    "reconcile_orphans",
    "remove_session_container",
    "run_command",
]
