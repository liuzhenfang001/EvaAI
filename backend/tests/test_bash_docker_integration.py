"""真 docker 的集成测试：跑一个真容器，验证假 runner **证不了**的那几件事。

假 runner 能钉住 argv 的形状，但钉不住「docker 真的收下了这些参数」——尤其是 **bind mount
在两个方向上真的通**（容器里写的文件出现在宿主的会话目录里），那是这片的核心不变量，也是
Windows 路径写法唯一能被证伪的地方。

**永不进 CI 必需路径**：没有 docker（或没有镜像）就整体跳过。跑它需要本机 Docker Desktop
与镜像，默认用下面这个 digest，可以用 ``EVA_TEST_SANDBOX_IMAGE`` 换。

每条用例用**唯一的会话 id**，夹具负责拆容器——不然跑几次就在 `docker ps` 里堆一排 evaai-*。
"""

import os
import shutil
import subprocess
import uuid

import pytest

from app.tools import docker_sandbox
from app.tools.bash import make_bash
from app.tools.docker_sandbox import DockerError, SandboxSpec, check_docker_ready, container_name, run_command

IMAGE = os.getenv(
    "EVA_TEST_SANDBOX_IMAGE",
    "sha256:6328d7fd2f0ff0b4c147c3d05b3df1ce331f4a482eb6e550ecd64ed1fcf906e7",
)


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "image", "inspect", IMAGE],
        capture_output=True,
        timeout=30,
        check=False,
    )
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(not _docker_available(), reason=f"本机没有 docker 或没有镜像 {IMAGE}")


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """把沙箱根换到 tmp_path，**不碰真的 backend/.evaai**，并备好哨兵文件。"""
    monkeypatch.setattr("app.tools.sandbox.SANDBOX_ROOT", tmp_path)
    docker_sandbox.ensure_mount_check()
    return tmp_path


@pytest.fixture
def session(sandbox):  # noqa: ARG001
    """唯一会话 id + 保证拆掉容器。

    依赖 ``sandbox`` 是有意的：拆容器按 **deployment label** 找，而那个 label 是从
    ``SANDBOX_ROOT`` 算出来的。夹具按依赖顺序逆向收尾，所以这里能赶在 SANDBOX_ROOT 被还回去
    之前拆干净——否则过滤条件对不上，容器会一直堆在 ``docker ps`` 里。
    """
    name = f"itest{uuid.uuid4().hex[:8]}"
    yield name
    try:
        docker_sandbox.remove_session_container(name)
    except Exception:  # pragma: no cover - 清理失败不该把测试结果染红
        pass


@pytest.fixture
def spec():
    return SandboxSpec(image=IMAGE, network="bridge", timeout=30, memory="1g", cpus="2", pids_limit=512)


def _run(session, command, spec):
    return docker_sandbox.mask(run_command(session, command, spec).output.decode("utf-8", errors="replace"))


def test_the_working_directory_is_the_virtual_workspace(session, spec):
    assert _run(session, "pwd", spec).strip() == "/workspace"


def test_the_image_brings_a_python_and_a_node(session, spec):
    """选镜像这件事在启动期校验过，这里再端到端确认一次。"""
    assert _run(session, "python3 -V", spec).startswith("Python 3")
    assert _run(session, "node -v", spec).startswith("v")


def test_a_file_written_in_the_container_lands_in_the_host_session_directory(session, sandbox, spec):
    """**这片的核心不变量**：容器内的 /workspace 就是文件工具看到的那个宿主目录。

    假 runner 永远证不了这条——它只看得见 argv。
    """
    _run(session, "echo '你好' > /workspace/from-container.txt", spec)

    written = sandbox / "user" / session / "workspace" / "from-container.txt"
    assert written.read_text(encoding="utf-8").strip() == "你好"


def test_a_file_put_on_the_host_is_visible_in_the_container(session, sandbox, spec):
    """反方向也要通——用户往 /input 放的原件，容器里读得到。"""
    upload = sandbox / "user" / session / "input" / "upload.txt"
    upload.parent.mkdir(parents=True, exist_ok=True)
    upload.write_text("来自用户\n", encoding="utf-8")

    assert _run(session, "cat /input/upload.txt", spec).strip() == "来自用户"


def test_input_and_skills_are_really_read_only(session, spec):
    """只读是 **docker 强制的**，不是我们自己拦下来的——这是本片相对软沙箱的增量。"""
    assert "Read-only file system" in _run(session, "touch /input/x 2>&1", spec)
    assert "Read-only file system" in _run(session, "touch /skills/x 2>&1", spec)


def test_a_nonzero_exit_is_reported_with_its_code(session, spec):
    result = run_command(session, "ls /workspace/nope", spec)

    assert result.returncode != 0
    assert "No such file" in result.output.decode("utf-8", errors="replace")


def test_the_container_keeps_running_between_calls(session, spec):
    """长驻的意义：**容器内**的状态（这里是 /tmp 下的文件）跨调用留存。"""
    _run(session, "echo hi > /tmp/evaai-marker", spec)

    assert _run(session, "cat /tmp/evaai-marker", spec).strip() == "hi"


def test_each_call_is_a_fresh_shell(session, spec):
    """但**shell 状态**不留存——`cd` 不跨调用（工具 docstring 里写明了，这里钉住）。"""
    _run(session, "cd /tmp", spec)

    assert _run(session, "pwd", spec).strip() == "/workspace"


def test_the_container_is_named_after_the_session_and_reused(session, spec):
    _run(session, "true", spec)
    _run(session, "true", spec)

    listed = subprocess.run(
        ["docker", "ps", "--filter", f"name=^{container_name(session)}$", "--format", "{{.ID}}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.split()

    assert len(listed) == 1


def test_a_command_that_overruns_the_timeout_is_stopped(session, spec):
    """容器内的 ``timeout`` 才是真边界——杀掉 docker 客户端杀不掉它。"""
    quick = SandboxSpec(image=spec.image, timeout=2, memory=spec.memory, cpus=spec.cpus, pids_limit=spec.pids_limit)

    result = run_command(session, "sleep 60; echo never", quick)

    assert result.returncode == 124
    assert "never" not in result.output.decode("utf-8", errors="replace")


def test_an_unknown_image_refuses_at_startup(sandbox):
    """决策 4：启用了却用不了 = 拒绝启动，不是静默降级。"""
    with pytest.raises(DockerError, match="不在本机"):
        check_docker_ready("evaai-nonexistent:0.0.0")


def test_the_factory_builds_a_usable_tool(session):
    """工厂这条路（config.yaml 走的就是它）也得能端到端跑通——包括它内部的启动期校验。"""
    import asyncio

    from langchain.tools import ToolRuntime

    tool = make_bash(image=IMAGE, timeout=30)
    runtime = ToolRuntime(
        state=None,
        context={"thread_id": f"{session}:lead"},
        config={},
        stream_writer=lambda _chunk: None,
        tool_call_id="call-1",
        store=None,
        tools=[],
    )

    assert asyncio.run(tool.ainvoke({"runtime": runtime, "command": "echo ok"})) == "ok"
