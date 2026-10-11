"""``bash`` 工具：在本**会话私有**的 Linux 容器里执行一条 shell 命令。

工具签名只有 ``command``——镜像、网络、内存、超时都是**构建期旋钮**（``config.yaml`` 里
bash 条目的额外键，启动期绑进工厂闭包），模型看不见也不该看见。与 ``web_search`` 的
``max_results`` 同一口径：那是运维的旋钮，不是模型的选择。

**非零退出码不是失败。** 那是命令的结果，照常返回并附上退出码；只有 docker 这一层失败
（CLI 不在、daemon 连不上、容器建不出来）才 raise。这是对 ADR-0012 §6「失败一律 raise」的
**有意偏离**：那条针对的是「工具没能完成它的工作」，而 shell 命令的非零退出是**结果**。
两种形状在文本上分得开——失败走中间件的 ``Error: ...``，成功走这里的输出 + ``[exit code N]``。

输出与异常用英文：模型侧本来就是英文（系统提示词的工具段、中间件的兜底句），工具跟着走。

渲染的三条规矩：**先掩码再截断**（截断可能把宿主根劈成两半，之后再掩码就漏了那半截）、
**保留头也保留尾**（命令的报错在尾巴上，这点与 ``file_ops`` 只截头不同）、**没有输出就说没有**
（空字符串会让模型以为工具坏了）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool

from evaharness.agents.middlewares.context import sandbox_layout, session_id
from evaharness.sandbox.docker import Completed, SandboxSpec, check_docker_ready, run_command
from evaharness.sandbox.paths import SandboxLayout

BASH_TOOL_NAME = "bash"

MAX_BASH_CHARS = 16_000
"""单次返回给模型的字符上限（与 ``file_ops.MAX_READ_CHARS`` 同一个预算）。

按**字符**算：中文一个字约一个 token，16k 字符已经吃掉 DeepSeek 64K 上下文四分之一。
"""

_TIMEOUT_EXIT_CODE = 124
"""GNU ``timeout`` 在超时后 SIGTERM 掉命令时的退出码。"""

_KILLED_EXIT_CODE = 137
"""``timeout -k`` 补的那刀（128+9）。也可能是容器 OOM——无法区分，文案把两种都提一句。"""


def make_bash(
    *,
    image: str,
    network: str = "bridge",
    timeout: int = 60,
    memory: str = "1g",
    cpus: str = "2",
    pids_limit: int = 512,
) -> BaseTool:
    """建出 ``bash`` 工具。**启动期**由注册表（`build_tools`）调用。

    Args:
        image: 容器镜像。必须已经在本机（``docker pull`` 过），且带 ``bash``/``timeout``/``sleep``。
        network: ``bridge``（可出网，默认）或 ``none``（断网）。
        timeout: 单条命令的秒数上限。容器内的 ``timeout`` 是真边界——杀掉 ``docker exec``
            客户端**杀不掉**容器里的进程，所以两边都要有。
        memory: 内存上限；等值设 ``--memory-swap``，不许靠 swap 吃穿上限。
        cpus: CPU 上限；空串表示不设。
        pids_limit: 进程数上限，防 ``:(){ :|:& };:`` 这类 fork 炸弹。

    Raises:
        DockerError: docker / 镜像 / 镜像能力任一不合格。**启动期拒绝启动**，不降级成 warning
            ——「启用了 bash 但 bash 跑不起来」是个坏部署，早说比晚说好。
    """
    check_docker_ready(image)
    spec = SandboxSpec(
        image=image,
        network=network,
        timeout=timeout,
        memory=memory,
        cpus=cpus,
        pids_limit=pids_limit,
    )

    @tool(parse_docstring=True)
    async def bash(runtime: ToolRuntime[Any], command: str) -> str:
        """Run a shell command in this session's own Linux container and return its output.

        The container sees the same four directories as the file tools — /workspace, /output,
        /input (read-only), /skills (read-only) — and they are literally the same files, so what
        you write with this tool is immediately visible to write_file / read_file and vice versa.
        Anything outside those four directories (installed packages, /tmp, background processes)
        is invisible to the other tools and is lost if the container is rebuilt, so write anything
        worth keeping to /workspace or /output.

        Each call is a fresh shell: cd and exported variables do not carry over to the next call.

        A non-zero exit code is a normal result, reported as "[exit code N]".

        Args:
            command: The shell command to run. It is handed to bash as one string, so pipes,
                quotes and redirections work as usual.
        """
        # 同步的 subprocess 丢进 worker 线程：不能阻塞事件循环（一条命令最长可以跑 timeout 秒）。
        layout = sandbox_layout(runtime)
        return await asyncio.to_thread(_execute, layout, session_id(runtime), command, spec)

    return bash


def _execute(layout: SandboxLayout, session: str, command: str, spec: SandboxSpec) -> str:
    return render(run_command(layout, session, command, spec), timeout=spec.timeout, layout=layout)


def render(completed: Completed, *, timeout: int, layout: SandboxLayout) -> str:
    """把一次调用的结果渲染成模型看到的文本。"""
    body = layout.mask(completed.output.decode("utf-8", errors="replace")).rstrip()

    if completed.truncated:
        # 读满上限被我们杀掉：退出码是我们造成的，报它只会误导。
        return _truncate(body)
    if completed.returncode == 0:
        return _truncate(body) if body else "(no output)"

    if completed.returncode == _TIMEOUT_EXIT_CODE:
        note = f"[timed out after {timeout}s; the command was stopped]"
    elif completed.returncode == _KILLED_EXIT_CODE:
        note = (
            f"[killed after {timeout}s — either the command ignored the timeout signal, or the "
            "container ran out of memory]"
        )
    else:
        note = f"[exit code {completed.returncode}]"

    # 超时的说明放**前面**：它解释了为什么下面的输出是半截的。退出码放**后面**：
    # 那是命令自己输出的收尾，模型按顺序读下来正好。
    if not body:
        return note
    if completed.returncode == _TIMEOUT_EXIT_CODE:
        return f"{note}\n\n{_truncate(body)}"
    return f"{_truncate(body)}\n\n{note}"


def _truncate(text: str) -> str:
    """超长就保头也保尾——命令的报错在尾巴上。"""
    if len(text) <= MAX_BASH_CHARS:
        return text
    head = MAX_BASH_CHARS * 3 // 4
    tail = MAX_BASH_CHARS - head
    note = f"… (truncated; showing first {head} and last {tail} of {len(text)} chars)"
    return f"{text[:head]}\n{note}\n{text[-tail:]}"


__all__ = ["BASH_TOOL_NAME", "MAX_BASH_CHARS", "make_bash", "render"]
