"""沙箱层：模型能碰哪些文件（虚拟路径），以及在哪儿跑命令（容器）。

两套机制，同一套坐标：

- :mod:`~evaharness.sandbox.paths` —— **虚拟路径**。模型只看得见 ``/workspace`` 这类虚拟根，
  宿主路径永远不进上下文。校验分两层（做 I/O 之前词法一遍、``resolve()`` 之后再一遍）。
- :mod:`~evaharness.sandbox.docker` —— **容器**。容器内路径**就是**虚拟路径，宿主目录经
  bind mount 映射进去，所以文件工具与容器里跑的命令看到的是同一份文件。

两个工具模块（:mod:`~evaharness.sandbox.file_ops` / :mod:`~evaharness.sandbox.bash`）建在
这两套机制上。

**这一层不含任何全局**：宿主根与挂载表由调用方构造一次
:class:`~evaharness.sandbox.paths.SandboxLayout`，经**运行上下文**注入（工具侧用
:func:`evaharness.agents.middlewares.context.sandbox_layout` 取）。所以「同一个框架、不同的
部署」在这里只是换一个 layout——这也是它与参照项目最关键的一处分歧（那边用进程级 provider 单例）。
"""

from __future__ import annotations

from evaharness.sandbox.bash import BASH_TOOL_NAME, MAX_BASH_CHARS, make_bash, render
from evaharness.sandbox.docker import (
    Completed,
    DockerCli,
    DockerError,
    Runner,
    SandboxSpec,
    check_docker_ready,
    container_name,
    deployment_id,
    ensure_mount_check,
    mount_check_dir,
    reconcile_orphans,
    remove_session_container,
    run_command,
)
from evaharness.sandbox.file_ops import glob, grep, ls, read_file, str_replace, write_file
from evaharness.sandbox.paths import (
    DEFAULT_MOUNT,
    DEFAULT_MOUNTS,
    Mount,
    PathNotAllowedError,
    Sandbox,
    SandboxLayout,
)

__all__ = [
    "BASH_TOOL_NAME",
    "DEFAULT_MOUNT",
    "DEFAULT_MOUNTS",
    "MAX_BASH_CHARS",
    "Completed",
    "DockerCli",
    "DockerError",
    "Mount",
    "PathNotAllowedError",
    "Runner",
    "Sandbox",
    "SandboxLayout",
    "SandboxSpec",
    "check_docker_ready",
    "container_name",
    "deployment_id",
    "ensure_mount_check",
    "glob",
    "grep",
    "ls",
    "make_bash",
    "mount_check_dir",
    "read_file",
    "reconcile_orphans",
    "remove_session_container",
    "render",
    "run_command",
    "str_replace",
    "write_file",
]
