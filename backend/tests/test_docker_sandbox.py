"""契约测试：docker 沙箱的机制层（命令行拼装、容器生命周期、清理）。

全部走假 runner——**单测不要求本机装 docker**。真 docker 的那部分在
``test_bash_docker_integration.py``（带 skipif）。夹具把 ``app.tools.sandbox.SANDBOX_ROOT``
换到 ``tmp_path``，于是挂载点的宿主路径是可控的，也顺带验证了「根是调用时读的」。
"""

import pytest
from tool_fakes import FakeDockerRunner

from app.tools import docker_sandbox, sandbox
from app.tools.docker_sandbox import Completed, DockerError, SandboxSpec, check_docker_ready, container_name

SESSION = "th-1"


@pytest.fixture(autouse=True)
def _sandbox_root(tmp_path, monkeypatch):
    monkeypatch.setattr("app.tools.sandbox.SANDBOX_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def runner():
    return FakeDockerRunner()


@pytest.fixture
def spec():
    return SandboxSpec(image="img:1", network="bridge", timeout=30, memory="1g", cpus="2", pids_limit=512)


def _run_command(runner, spec, *, session=SESSION, command="echo hi"):
    return docker_sandbox.run_command(session, command, spec, runner=runner)


# --------------------------------------------------------------------------- #
# 命令行拼装
# --------------------------------------------------------------------------- #


def test_the_container_mounts_all_four_paths_at_their_virtual_names(runner, spec, tmp_path):
    """**这是整片的核心不变量**：宿主目录 bind 到容器里的**虚拟路径**。"""
    _run_command(runner, spec)
    argv = runner.find("run")

    specs = [argv[index + 1] for index, arg in enumerate(argv) if arg == "-v"]
    assert f"{tmp_path / 'user' / SESSION / 'workspace'}:/workspace".replace("\\", "/") in specs
    assert f"{tmp_path / 'user' / SESSION / 'output'}:/output".replace("\\", "/") in specs
    assert f"{tmp_path / 'user' / SESSION / 'input'}:/input:ro".replace("\\", "/") in specs
    assert f"{tmp_path / 'skills-view'}:/skills:ro".replace("\\", "/") in specs
    assert f"{tmp_path / '.mount-check'}:/evaai-check:ro".replace("\\", "/") in specs


def test_input_and_skills_are_read_only_at_the_docker_level(runner, spec):
    """只读从「工具自己遵守的约定」变成 **docker 强制的边界**——这是本片的意义所在。"""
    _run_command(runner, spec)
    specs = [arg for arg in runner.find("run") if ":/input" in arg or ":/skills" in arg]

    assert len(specs) == 2
    assert all(spec.endswith(":ro") for spec in specs)


def test_the_host_path_is_handed_to_docker_with_forward_slashes(runner, spec):
    """Windows 回归守卫：``D:\\a\\b`` 的反斜杠在 ``宿主:容器`` 里容易被误读。"""
    _run_command(runner, spec)
    specs = [arg for arg in runner.find("run") if ":/" in arg and arg.startswith(("C:", "D:", "/", "c:", "d:"))]

    assert specs
    assert all("\\" not in spec for spec in specs)


def test_the_container_carries_the_labels_and_the_resource_limits(runner, spec):
    _run_command(runner, spec)
    argv = runner.find("run")

    assert ["--name", container_name(SESSION)] == argv[argv.index("--name") : argv.index("--name") + 2]
    assert "evaai.managed=1" in argv
    assert f"evaai.deployment={docker_sandbox.deployment_id()}" in argv
    assert f"evaai.session={SESSION}" in argv
    assert ["--network", "bridge"] == argv[argv.index("--network") : argv.index("--network") + 2]
    assert "1g" in argv and argv.count("1g") == 2  # --memory 与 --memory-swap 等值，不许靠 swap 吃穿
    assert "2" in argv
    assert "512" in argv
    assert "no-new-privileges" in argv
    # 镜像的 HEALTHCHECK 探的是我们不启动的那套服务，不关掉容器会永远显示 unhealthy。
    assert "--no-healthcheck" in argv
    assert argv[argv.index("--workdir") + 1] == "/workspace"


def test_the_image_entrypoint_is_overridden_so_its_services_do_not_start(runner, spec):
    """AIO 镜像的 entrypoint 是 /opt/gem/run.sh（Jupyter/VNC/浏览器一整套），必须盖掉。"""
    _run_command(runner, spec)
    argv = runner.find("run")

    assert argv[argv.index("--entrypoint") + 1] == "sleep"
    assert argv[-1] == "infinity"
    assert argv[-2] == "img:1"


def test_the_command_is_wrapped_in_an_inner_timeout(runner, spec):
    """杀 ``docker exec`` 客户端杀不掉容器里的进程——容器内的 ``timeout`` 才是真边界。"""
    _run_command(runner, spec, command="sleep 99")
    argv = runner.find("exec")

    assert argv[argv.index("timeout") :] == [
        "timeout",
        "-k",
        "5",
        "30",
        "bash",
        "-c",
        "sleep 99",
    ]


def test_the_command_is_one_argv_element(runner, spec):
    """引号、管道、重定向交给容器里的 bash 解析，我们不经手也不加壳。"""
    _run_command(runner, spec, command='echo "a b" | wc -l')

    assert runner.find("exec")[-1] == 'echo "a b" | wc -l'


# --------------------------------------------------------------------------- #
# 生命周期
# --------------------------------------------------------------------------- #


def test_the_container_is_created_lazily_and_reused(runner, spec):
    _run_command(runner, spec)
    _run_command(runner, spec)

    assert runner.verbs[0] == "exec"  # 先试 exec——不为每次调用付一次 inspect/run
    assert runner.verbs.count("run") == 1  # 第二个调用复用了同一个容器


def test_a_lost_race_to_create_the_container_reuses_the_winner(runner, spec):
    """并发首次调用：docker 的全局名字唯一性就是互斥锁，撞名的那个直接复用。"""
    runner.create_conflict = True

    result = _run_command(runner, spec)

    assert result.returncode == 0
    assert runner.verbs.count("run") == 1


def test_a_dead_container_is_recreated_once(runner, spec):
    # 先建出来，再从「存在」集合里拿掉——模拟容器被 docker 之外的什么清掉了。
    _run_command(runner, spec)
    runner.existing.clear()

    result = _run_command(runner, spec)

    assert result.returncode == 0
    assert runner.verbs.count("run") == 2  # 只重建一次，不是每次调用都重建


def test_a_failed_create_is_a_docker_error(runner, spec):
    runner.create_error = b"Error response from daemon: pull access denied"

    with pytest.raises(DockerError, match="could not start the sandbox container"):
        _run_command(runner, spec)


def test_an_unshared_drive_is_reported_loudly(runner, spec):
    """Docker Desktop 没共享盘符时 ``-v`` 静默变空目录——哨兵探测把它变成响的。"""
    runner.probe_returncode = 1

    with pytest.raises(DockerError, match="File Sharing"):
        _run_command(runner, spec)

    assert runner.verbs[-1] == "rm"  # 别把那个看不到文件的坏容器留在 ps 里


def test_a_nonzero_exit_is_not_a_docker_error(runner, spec):
    """命令失败是**结果**，不是 docker 层失败——照常返回。"""
    runner.command_result = Completed(returncode=2, output=b"boom")

    assert _run_command(runner, spec).returncode == 2


def test_a_command_not_found_exit_code_is_not_mistaken_for_a_missing_container(runner, spec):
    """容器内的 127 是 bash 在说「没这个命令」，绝不能当成「容器没了」去重建。"""
    _run_command(runner, spec)  # 先让容器建出来
    runner.command_result = Completed(returncode=127, output=b"bash: nope: command not found")

    _run_command(runner, spec)

    assert runner.verbs.count("run") == 1


def test_docker_errors_do_not_leak_the_host_path(runner, spec, tmp_path):
    runner.create_error = f"Error response from daemon: invalid mount path {tmp_path}\\user".encode()

    with pytest.raises(DockerError) as caught:
        _run_command(runner, spec)

    assert str(tmp_path) not in str(caught.value)
    assert str(tmp_path).replace("\\", "/") not in str(caught.value)


# --------------------------------------------------------------------------- #
# 启动期校验
# --------------------------------------------------------------------------- #


def test_startup_refuses_when_the_docker_cli_is_missing(monkeypatch):
    monkeypatch.setattr(docker_sandbox.shutil, "which", lambda _name: None)

    with pytest.raises(DockerError, match="找不到 docker"):
        check_docker_ready("img:1", runner=FakeDockerRunner())


def test_startup_refuses_when_the_daemon_is_unreachable(monkeypatch):
    monkeypatch.setattr(docker_sandbox.shutil, "which", lambda _name: "/usr/bin/docker")
    runner = FakeDockerRunner()
    runner.invoke = lambda argv, *, timeout: Completed(returncode=1, output=b"cannot connect")  # type: ignore[method-assign]

    with pytest.raises(DockerError, match="daemon 连不上"):
        check_docker_ready("img:1", runner=runner)


def test_startup_refuses_when_the_image_is_missing(monkeypatch):
    monkeypatch.setattr(docker_sandbox.shutil, "which", lambda _name: "/usr/bin/docker")
    runner = FakeDockerRunner()
    calls: list[list[str]] = []

    def invoke(argv, *, timeout):
        calls.append(argv)
        if argv[1] == "image":
            return Completed(returncode=1, output=b"No such image")
        return Completed(returncode=0, output=b"")

    runner.invoke = invoke  # type: ignore[method-assign]

    with pytest.raises(DockerError, match="不在本机"):
        check_docker_ready("img:1", runner=runner)


def test_startup_refuses_when_the_image_lacks_bash(monkeypatch):
    monkeypatch.setattr(docker_sandbox.shutil, "which", lambda _name: "/usr/bin/docker")
    runner = FakeDockerRunner()

    def invoke(argv, *, timeout):
        if argv[1] == "run":
            return Completed(returncode=127, output=b"")
        return Completed(returncode=0, output=b"")

    runner.invoke = invoke  # type: ignore[method-assign]

    with pytest.raises(DockerError, match="缺 bash"):
        check_docker_ready("img:1", runner=runner)


def test_startup_creates_the_sentinel_file(monkeypatch, tmp_path):
    """哨兵文件必须真的存在，否则每次建容器都会误判成「盘符没共享」。"""
    monkeypatch.setattr(docker_sandbox.shutil, "which", lambda _name: "/usr/bin/docker")

    check_docker_ready("img:1", runner=FakeDockerRunner())

    assert (tmp_path / ".mount-check" / "ok").is_file()


# --------------------------------------------------------------------------- #
# 清理
# --------------------------------------------------------------------------- #


def test_reconcile_removes_only_the_sessions_missing_from_the_journal(runner):
    runner.ps_lines = ["cid-alive\tth-alive", "cid-gone\tth-gone"]

    removed = docker_sandbox.reconcile_orphans({"th-alive"}, runner=runner)

    assert removed == 1
    assert ["docker", "rm", "-f", "cid-gone"] in runner.calls


def test_reconcile_scopes_itself_to_this_deployment(runner):
    """同机跑两个实例时，不能去删另一个部署的容器。"""
    docker_sandbox.reconcile_orphans(set(), runner=runner)

    ps = runner.find("ps")
    assert f"label=evaai.deployment={docker_sandbox.deployment_id()}" in ps
    assert "label=evaai.managed=1" in ps


def test_reconcile_never_raises(runner):
    def boom(argv, *, timeout):
        raise OSError("docker 不见了")

    runner.invoke = boom  # type: ignore[method-assign]

    assert docker_sandbox.reconcile_orphans(set(), runner=runner) == 0


def test_reconcile_survives_a_failing_removal(runner):
    runner.ps_lines = ["cid-gone\tth-gone"]

    def invoke(argv, *, timeout):
        if argv[1] == "rm":
            return Completed(returncode=1, output=b"device or resource busy")
        return Completed(returncode=0, output="\n".join(runner.ps_lines).encode())

    runner.invoke = invoke  # type: ignore[method-assign]

    assert docker_sandbox.reconcile_orphans(set(), runner=runner) == 0


def test_removing_a_session_container_only_targets_that_session(runner):
    runner.ps_lines = ["cid-1\tth-1"]

    removed = docker_sandbox.remove_session_container(SESSION, runner=runner)

    assert removed == 1
    assert f"label=evaai.session={SESSION}" in runner.find("ps")
    assert ["docker", "rm", "-f", "cid-1"] in runner.calls


def test_the_container_name_is_derived_from_the_session():
    """名字由会话推出 = 不需要任何「会话 → 容器」的内存映射。"""
    assert container_name("th-1") == "evaai-th-1"


def test_the_deployment_id_is_stable_across_calls():
    assert docker_sandbox.deployment_id() == docker_sandbox.deployment_id()


def test_the_mount_check_dir_sits_under_the_sandbox_root(tmp_path):
    assert docker_sandbox.mount_check_dir() == sandbox.SANDBOX_ROOT / ".mount-check"
