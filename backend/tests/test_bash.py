"""契约测试：``bash`` 工具（模型看到的返回形状 + 工具接线）。

渲染部分是**纯函数**，直接调；工具接线靠 monkeypatch 掉 ``run_command``/``check_docker_ready``
这两个协作方——单测不需要本机装 docker（真 docker 的在 ``test_bash_docker_integration.py``）。
"""

import asyncio
from pathlib import Path

import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool
from tool_fakes import fake_runtime, lead_context

from evaharness.sandbox import MAX_BASH_CHARS, Completed, SandboxLayout
from evaharness.sandbox import bash as bash_module

_PLAIN_LAYOUT = SandboxLayout(root=Path("C:/evaai-plain"))
"""渲染用例的默认布局。

那些用例断言的是**返回形状**（退出码、截断、超时提示），不关心掩码——掩码自己有专项测试，
它会传一个真的 tmp_path 进来。所以这里给个不会命中任何正文的常量根就够。
"""


@pytest.fixture
def layout(tmp_path):
    """这次运行的沙箱布局（根指向 tmp_path）——掩码与工具接线都要它。"""
    return SandboxLayout(root=tmp_path)


@pytest.fixture
def tool(monkeypatch, layout):
    """建出工具，但把启动期校验换成空操作——校验自己另有测试。"""
    monkeypatch.setattr(bash_module, "check_docker_ready", lambda image: None)
    return bash_module.make_bash(image="img:1")


def _render(*, returncode=0, output=b"", truncated=False, timeout=60, layout=_PLAIN_LAYOUT):
    return bash_module.render(
        Completed(returncode=returncode, output=output, truncated=truncated), timeout=timeout, layout=layout
    )


def _invoke(tool, command="echo hi", layout=_PLAIN_LAYOUT):
    runtime = fake_runtime(lead_context(layout))
    return asyncio.run(tool.ainvoke({"runtime": runtime, "command": command}))


# --------------------------------------------------------------------------- #
# 工具接线
# --------------------------------------------------------------------------- #


def test_the_schema_only_exposes_the_command(tool):
    """配置是唯一来源——镜像、网络、内存、超时都不该模型看得见（工厂形态的主要收益）。"""
    properties = convert_to_openai_tool(tool)["function"]["parameters"].get("properties", {})

    assert set(properties) == {"command"}
    for knob in ("image", "network", "timeout", "memory", "cpus", "pids_limit"):
        assert knob not in properties


def test_the_command_runs_in_this_sessions_container(tool, monkeypatch):
    seen: dict = {}

    def fake_run(layout, session, command, spec):
        seen.update(layout=layout, session=session, command=command, spec=spec)
        return Completed(returncode=0, output=b"hello\n")

    monkeypatch.setattr(bash_module, "run_command", fake_run)

    assert _invoke(tool, "echo hello") == "hello"
    assert seen["layout"].root == _PLAIN_LAYOUT.root
    assert seen["session"] == "th-1"
    assert seen["command"] == "echo hello"
    assert seen["spec"].image == "img:1"


# --------------------------------------------------------------------------- #
# 返回形状
# --------------------------------------------------------------------------- #


def test_a_successful_command_returns_just_its_output():
    assert _render(output=b"total 0\n") == "total 0"


def test_a_command_with_no_output_says_so():
    """空串会让模型以为工具坏了。"""
    assert _render(output=b"") == "(no output)"
    assert _render(output=b"\n\n") == "(no output)"


def test_a_nonzero_exit_is_a_normal_result_with_the_code_last():
    """命令失败是**结果**不是错误——照常返回，退出码单独一行放最后。"""
    rendered = _render(returncode=2, output=b"ls: nope: No such file\n")

    assert rendered == "ls: nope: No such file\n\n[exit code 2]"


def test_a_nonzero_exit_with_no_output_still_reports_the_code():
    assert _render(returncode=1, output=b"") == "[exit code 1]"


def test_a_timeout_is_reported_before_the_partial_output():
    """说明放前面——它解释了为什么下面的输出是半截的。"""
    rendered = _render(returncode=124, output=b"partial\n", timeout=30)

    assert rendered == "[timed out after 30s; the command was stopped]\n\npartial"


def test_a_sigkill_mentions_both_possible_causes():
    """137 既可能是 timeout -k 补的那刀，也可能是容器 OOM——分不出来的事不装作分得出。"""
    rendered = _render(returncode=137, output=b"", timeout=30)

    assert "killed after 30s" in rendered
    assert "out of memory" in rendered


def test_stderr_is_merged_into_the_same_stream():
    """一个线程读两个管道会死锁，所以合并——终端本来就是合并的。"""
    assert _render(output=b"out\nerr\n") == "out\nerr"


# --------------------------------------------------------------------------- #
# 截断与掩码
# --------------------------------------------------------------------------- #


def test_long_output_keeps_both_the_head_and_the_tail():
    """命令的报错在**尾巴**上——这点与 file_ops 只截头不同。"""
    body = "A" * MAX_BASH_CHARS + "TAIL-MARKER"
    rendered = _render(output=body.encode())

    assert rendered.startswith("A")
    assert rendered.endswith("TAIL-MARKER")
    assert "truncated" in rendered


def test_truncation_stays_within_the_budget():
    rendered = _render(output=("x" * (MAX_BASH_CHARS * 3)).encode())
    body_lines = [line for line in rendered.splitlines() if "truncated" not in line]

    assert sum(len(line) for line in body_lines) <= MAX_BASH_CHARS + 2


def test_read_cap_truncation_does_not_report_an_exit_code():
    """读满上限是我们自己杀的，退出码是我们造成的——报它只会误导。"""
    rendered = _render(returncode=-9, output=b"lots", truncated=True)

    assert "exit code" not in rendered


def test_invalid_utf8_does_not_raise():
    """命令输出可以是任意字节。"""
    assert "bad" in _render(output=b"bad \xff\xfe bytes")


@pytest.mark.parametrize("form", ["{root}", "{slash}", "{lower}", "{extended}"])
def test_host_paths_are_masked_in_every_spelling_docker_might_use(form, tmp_path):
    layout = SandboxLayout(root=tmp_path)
    root = str(tmp_path)
    variants = {
        "{root}": root,
        "{slash}": root.replace("\\", "/"),
        "{lower}": root[0].lower() + root[1:],
        "{extended}": "\\\\?\\" + root,
    }

    rendered = _render(output=f"docker said: {variants[form]}".encode(), layout=layout)

    assert root not in rendered
    assert root.replace("\\", "/") not in rendered
    assert "<evaai>" in rendered
