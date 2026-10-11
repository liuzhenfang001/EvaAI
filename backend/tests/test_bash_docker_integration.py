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

from evaharness.sandbox import (
    DockerError,
    SandboxLayout,
    SandboxSpec,
    check_docker_ready,
    container_name,
    ensure_mount_check,
    make_bash,
    remove_session_container,
    run_command,
)

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
def sandbox(tmp_path):
    """沙箱布局指向 ``tmp_path``（**不碰真的 backend/.evaai**），并备好哨兵文件。

    哨兵由 app 在 lifespan 里建（`check_docker_ready` 拿不到布局），测试里就自己建一次。
    """
    layout = SandboxLayout(root=tmp_path)
    ensure_mount_check(layout)
    return layout


@pytest.fixture
def session(sandbox):  # noqa: ARG001
    """唯一会话 id + 保证拆掉容器。

    依赖 ``sandbox``（布局）是有意的：拆容器按 **deployment label** 找，而那个 label 由布局
    的宿主根算出。夹具按依赖顺序逆向收尾，所以这里用的是同一次运行那个布局。
    """
    name = f"itest{uuid.uuid4().hex[:8]}"
    yield name
    try:
        remove_session_container(sandbox, name)
    except Exception:  # pragma: no cover - 清理失败不该把测试结果染红
        pass


@pytest.fixture
def spec():
    return SandboxSpec(image=IMAGE, network="bridge", timeout=30, memory="1g", cpus="2", pids_limit=512)


def _run(session, command, spec, layout):
    result = run_command(layout, session, command, spec)
    return layout.mask(result.output.decode("utf-8", errors="replace"))


def test_the_working_directory_is_the_virtual_workspace(session, sandbox, spec):
    assert _run(session, "pwd", spec, sandbox).strip() == "/workspace"


def test_the_image_brings_a_python_and_a_node(session, sandbox, spec):
    """选镜像这件事在启动期校验过，这里再端到端确认一次。"""
    assert _run(session, "python3 -V", spec, sandbox).startswith("Python 3")
    assert _run(session, "node -v", spec, sandbox).startswith("v")


def test_a_file_written_in_the_container_lands_in_the_host_session_directory(session, sandbox, spec):
    """**这片的核心不变量**：容器内的 /workspace 就是文件工具看到的那个宿主目录。

    假 runner 永远证不了这条——它只看得见 argv。
    """
    _run(session, "echo '你好' > /workspace/from-container.txt", spec, sandbox)

    written = sandbox.root / "user" / session / "workspace" / "from-container.txt"
    assert written.read_text(encoding="utf-8").strip() == "你好"


def test_a_file_put_on_the_host_is_visible_in_the_container(session, sandbox, spec):
    """反方向也要通——用户往 /input 放的原件，容器里读得到。"""
    upload = sandbox.root / "user" / session / "input" / "upload.txt"
    upload.parent.mkdir(parents=True, exist_ok=True)
    upload.write_text("来自用户\n", encoding="utf-8")

    assert _run(session, "cat /input/upload.txt", spec, sandbox).strip() == "来自用户"


def test_input_and_skills_are_really_read_only(session, sandbox, spec):
    """只读是 **docker 强制的**，不是我们自己拦下来的——这是本片相对软沙箱的增量。"""
    assert "Read-only file system" in _run(session, "touch /input/x 2>&1", spec, sandbox)
    assert "Read-only file system" in _run(session, "touch /skills/x 2>&1", spec, sandbox)


def test_a_nonzero_exit_is_reported_with_its_code(session, sandbox, spec):
    result = run_command(sandbox, session, "ls /workspace/nope", spec)

    assert result.returncode != 0
    assert "No such file" in result.output.decode("utf-8", errors="replace")


def test_the_container_keeps_running_between_calls(session, sandbox, spec):
    """长驻的意义：**容器内**的状态（这里是 /tmp 下的文件）跨调用留存。"""
    _run(session, "echo hi > /tmp/evaai-marker", spec, sandbox)

    assert _run(session, "cat /tmp/evaai-marker", spec, sandbox).strip() == "hi"


def test_each_call_is_a_fresh_shell(session, sandbox, spec):
    """但**shell 状态**不留存——`cd` 不跨调用（工具 docstring 里写明了，这里钉住）。"""
    _run(session, "cd /tmp", spec, sandbox)

    assert _run(session, "pwd", spec, sandbox).strip() == "/workspace"


def test_the_container_is_named_after_the_session_and_reused(session, sandbox, spec):
    _run(session, "true", spec, sandbox)
    _run(session, "true", spec, sandbox)

    listed = subprocess.run(
        ["docker", "ps", "--filter", f"name=^{container_name(session)}$", "--format", "{{.ID}}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.split()

    assert len(listed) == 1


def test_a_command_that_overruns_the_timeout_is_stopped(session, sandbox, spec):
    """容器内的 ``timeout`` 才是真边界——杀掉 docker 客户端杀不掉它。"""
    quick = SandboxSpec(image=spec.image, timeout=2, memory=spec.memory, cpus=spec.cpus, pids_limit=spec.pids_limit)

    result = run_command(sandbox, session, "sleep 60; echo never", quick)

    assert result.returncode == 124
    assert "never" not in result.output.decode("utf-8", errors="replace")


def test_an_unknown_image_refuses_at_startup(sandbox):
    """决策 4：启用了却用不了 = 拒绝启动，不是静默降级。"""
    with pytest.raises(DockerError, match="不在本机"):
        check_docker_ready("evaai-nonexistent:0.0.0")


def test_the_factory_builds_a_usable_tool(session, sandbox):
    """工厂这条路（config.yaml 走的就是它）也得能端到端跑通——包括它内部的启动期校验。"""
    import asyncio

    from langchain.tools import ToolRuntime

    tool = make_bash(image=IMAGE, timeout=30)
    runtime = ToolRuntime(
        state=None,
        context={"thread_id": f"{session}:lead", "sandbox": sandbox},
        config={},
        stream_writer=lambda _chunk: None,
        tool_call_id="call-1",
        store=None,
        tools=[],
    )

    assert asyncio.run(tool.ainvoke({"runtime": runtime, "command": "echo ok"})) == "ok"
