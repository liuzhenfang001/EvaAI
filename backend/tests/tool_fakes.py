"""工具测试的共用替身。

``ToolRuntime`` 是个 dataclass，除了 ``execution_info`` / ``server_info`` 之外都必填，
所以能直接手搓——工具的单元测试不该为了拿一个 ``context`` 去跑一整张图。
"""

from __future__ import annotations

from typing import Any

from langchain.tools import ToolRuntime

from app.tools.docker_sandbox import Completed
from evaharness.agents.lead_agent import LeadContext

WORKSPACE_CONTEXT = LeadContext(session_id="th-1", thread_id="th-1:lead", run_id="run-1")
"""lead agent 与子 agent 的形状（带类型的对象）。"""

COORDINATE_CONTEXT: dict[str, Any] = {"thread_id": "th-1:run-1:t1"}
"""**坐标形状**的 context：只有 ``thread_id``，没有 ``session_id``。

T16 下线 Plan-Exec 之前，这是它执行单元的形状（那条路径的图没有 ``context_schema``）。
路径没了，但形状还在契约里：``middlewares.context`` 的两个访问器都同时认对象与 dict，
而中间件的单测一直用裸 dict——所以这条要留着继续钉住。
"""


def fake_runtime(context: Any, *, tool_call_id: str = "call-1") -> ToolRuntime:
    """造一个够用的 ``ToolRuntime``。"""
    return ToolRuntime(
        state=None,
        context=context,
        config={},
        stream_writer=lambda _chunk: None,
        tool_call_id=tool_call_id,
        store=None,
        tools=[],
    )


class FakeDockerRunner:
    """假的 docker 命令行：把每次 argv 记下来，按子命令回一个可控的结果。

    有了它，docker 沙箱的单测**不需要本机装 docker**（与 ``test_web_tools`` 在 httpx 边界打桩
    同一手法）。真 docker 的那部分验证在 ``test_bash_docker_integration.py`` 里，带 ``skipif``。
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.existing: set[str] = set()
        """「已经在跑」的容器名——``exec`` 到这个集合之外的名字会得到 docker 的 No such container。"""

        self.command_result: Completed = Completed(returncode=0, output=b"")
        """``exec`` 跑真命令时的返回值（哨兵探测不走这里）。"""

        self.create_error: bytes | None = None
        """设了就让 ``docker run`` 失败并回这段文本（模拟镜像坏了这类真失败）。"""

        self.create_conflict = False
        """设了就让 ``docker run`` 撞名失败——但容器**已经存在**（模拟并发时另一个请求刚建好）。"""

        self.probe_returncode = 0
        """哨兵探测（``exec … test -f /.evaai-check/ok``）的退出码。"""

        self.ps_lines: list[str] = []
        self.ps_returncode = 0

    @property
    def verbs(self) -> list[str]:
        """每次调用的子命令，例如 ``["run", "exec"]``——用来断言「第二次不再建容器」。"""
        return [argv[1] for argv in self.calls if len(argv) > 1]

    def find(self, verb: str) -> list[str]:
        """第一条该子命令的 argv。"""
        return next(argv for argv in self.calls if len(argv) > 1 and argv[1] == verb)

    def invoke(self, argv: list[str], *, timeout: float) -> Completed:
        self.calls.append(list(argv))
        verb = argv[1] if len(argv) > 1 else ""
        if verb == "run":
            return self._run(argv)
        if verb == "exec":
            return self._exec(argv)
        if verb == "ps":
            return self._ps(argv)
        if verb == "rm":
            self.existing.discard(argv[-1])
            return Completed(returncode=0, output=b"")
        return Completed(returncode=0, output=b"")

    def _ps(self, argv: list[str]) -> Completed:
        """按请求的 ``--format`` 回结果——格式不同，行数/字段就不同。"""
        if self.ps_returncode != 0:
            return Completed(returncode=self.ps_returncode, output=b"")
        fmt = argv[argv.index("--format") + 1] if "--format" in argv else ""
        lines = [line.split("\t")[0] for line in self.ps_lines] if fmt == "{{.ID}}" else self.ps_lines
        return Completed(returncode=0, output="\n".join(lines).encode())

    def _run(self, argv: list[str]) -> Completed:
        if self.create_conflict:
            # 撞名 = 容器已经被另一个并发请求建出来了，所以它在 existing 里。
            self.existing.add(argv[argv.index("--name") + 1])
            return Completed(returncode=1, output=b'Conflict. The container name is already in use.')
        if self.create_error is not None:
            return Completed(returncode=1, output=self.create_error)
        if "--name" in argv:  # 启动期探针那次 run 是一次性的，不带名字
            self.existing.add(argv[argv.index("--name") + 1])
        return Completed(returncode=0, output=b"container-id\n")

    def _exec(self, argv: list[str]) -> Completed:
        if "test" in argv:  # 哨兵探测
            return Completed(returncode=self.probe_returncode, output=b"")
        name = next((arg for arg in argv if arg.startswith("evaai-")), "")
        if name not in self.existing:
            return Completed(returncode=1, output=b"Error response from daemon: No such container: " + name.encode())
        return self.command_result


__all__ = ["COORDINATE_CONTEXT", "WORKSPACE_CONTEXT", "FakeDockerRunner", "fake_runtime"]
