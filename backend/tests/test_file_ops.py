"""契约测试：六个文件工具。

全部在 ``tmp_path`` 上跑——**绝不碰真的 ``backend/.evaai``**。夹具把
``app.tools.sandbox.SANDBOX_ROOT`` 换掉，这同时验证了「根目录是调用时读的」：如果哪天有人
把它写成默认参数值，这些测试会一起变红（import 时求值就再也 monkeypatch 不动了）。
"""

import pytest
from tool_fakes import COORDINATE_CONTEXT, WORKSPACE_CONTEXT, fake_runtime

from app.tools import file_ops
from app.tools.sandbox import PathNotAllowedError


@pytest.fixture(autouse=True)
def _sandbox_root(tmp_path, monkeypatch):
    monkeypatch.setattr("app.tools.sandbox.SANDBOX_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def rt():
    return fake_runtime(WORKSPACE_CONTEXT)


def _run(tool, rt, **kwargs):  # noqa: ANN001, ANN202
    return tool.invoke({"runtime": rt, **kwargs})


# --------------------------------------------------------------------------- #
# write_file / read_file
# --------------------------------------------------------------------------- #


def test_write_then_read_round_trips(_sandbox_root, rt):
    assert _run(file_ops.write_file, rt, path="notes/todo.md", content="第一行\n第二行").startswith("OK")

    assert (_sandbox_root / "user" / "th-1" / "workspace" / "notes" / "todo.md").is_file()
    out = _run(file_ops.read_file, rt, path="/workspace/notes/todo.md")
    assert "第一行" in out and "第二行" in out


def test_write_creates_missing_parents(_sandbox_root, rt):
    _run(file_ops.write_file, rt, path="a/b/c/d.txt", content="x")
    assert (_sandbox_root / "user" / "th-1" / "workspace" / "a" / "b" / "c" / "d.txt").is_file()


def test_a_single_write_over_the_limit_is_refused_with_a_way_out(rt):
    """错误必须是**可操作**的：只说「太大了」等于让模型重试同一个调用。"""
    with pytest.raises(ValueError, match="append=True"):
        _run(file_ops.write_file, rt, path="big.txt", content="x" * (81 * 1024))


def test_append_bypasses_the_limit(rt):
    _run(file_ops.write_file, rt, path="big.txt", content="x" * (60 * 1024))
    tail = _run(file_ops.write_file, rt, path="big.txt", content="y" * (60 * 1024), append=True)
    assert tail.startswith("OK")


def test_read_file_honours_a_line_range(rt):
    _run(file_ops.write_file, rt, path="n.txt", content="\n".join(f"L{i}" for i in range(1, 11)))

    out = _run(file_ops.read_file, rt, path="n.txt", start_line=3, end_line=5)
    assert "L3" in out and "L5" in out
    assert "L2" not in out and "L6" not in out


def test_read_file_truncates_and_points_at_the_line_range(rt):
    _run(file_ops.write_file, rt, path="long.txt", content="y" * (file_ops.MAX_READ_CHARS + 500))

    out = _run(file_ops.read_file, rt, path="long.txt")
    assert len(out) < file_ops.MAX_READ_CHARS + 300
    assert "start_line" in out and "truncated" in out


def test_read_file_on_an_empty_file_is_not_an_error(rt):
    _run(file_ops.write_file, rt, path="empty.txt", content="")
    assert "empty" in _run(file_ops.read_file, rt, path="empty.txt")


def test_read_file_refuses_binary_content(rt):
    _run(file_ops.write_file, rt, path="bin.dat", content="ok")
    # 直接落一段非法 UTF-8，绕过 write_file 的文本参数
    (file_ops._sandbox(rt).resolve("/workspace/bin.dat")).write_bytes(b"\xff\xfe\x00\x01")

    with pytest.raises(ValueError, match="binary"):
        _run(file_ops.read_file, rt, path="bin.dat")


def test_read_file_on_a_missing_path_says_so_and_points_at_ls(rt):
    """「不存在」与「是目录」要分开说——模型看到前者该去 ls，看到后者该换工具。"""
    with pytest.raises(ValueError, match="does not exist"):
        _run(file_ops.read_file, rt, path="/workspace/nope.txt")


def test_read_file_on_a_directory_says_so(rt):
    _run(file_ops.write_file, rt, path="sub/x.txt", content="1")
    with pytest.raises(ValueError, match="is a directory"):
        _run(file_ops.read_file, rt, path="/workspace/sub")


# --------------------------------------------------------------------------- #
# str_replace
# --------------------------------------------------------------------------- #


def test_str_replace_requires_exactly_one_occurrence(rt):
    """**偏离 DeerFlow**：它的 docstring 写着唯一，代码却是「替换第一处」。

    静默改错文件不可逆，所以这里 0 次与多次都报错，而且**一个字节都不改**。
    """
    _run(file_ops.write_file, rt, path="d.txt", content="alpha beta alpha")

    with pytest.raises(ValueError, match="appears 2 times"):
        _run(file_ops.str_replace, rt, path="d.txt", old_str="alpha", new_str="X")

    assert "alpha beta alpha" in _run(file_ops.read_file, rt, path="d.txt")


def test_str_replace_all_replaces_every_occurrence(rt):
    _run(file_ops.write_file, rt, path="d.txt", content="alpha beta alpha")
    _run(file_ops.str_replace, rt, path="d.txt", old_str="alpha", new_str="X", replace_all=True)

    assert "X beta X" in _run(file_ops.read_file, rt, path="d.txt")


def test_str_replace_missing_pattern_changes_nothing(rt):
    _run(file_ops.write_file, rt, path="d.txt", content="hello")
    with pytest.raises(ValueError, match="not found"):
        _run(file_ops.str_replace, rt, path="d.txt", old_str="zzz", new_str="X")
    assert "hello" in _run(file_ops.read_file, rt, path="d.txt")


# --------------------------------------------------------------------------- #
# ls / glob / grep
# --------------------------------------------------------------------------- #


def test_ls_renders_a_two_level_tree(rt):
    _run(file_ops.write_file, rt, path="notes/a.md", content="x")
    _run(file_ops.write_file, rt, path="top.txt", content="y")

    out = _run(file_ops.ls, rt, path="/workspace")
    assert out.startswith("/workspace/")
    assert "notes/" in out and "a.md" in out and "top.txt" in out


def test_a_fresh_session_already_has_its_directories(_sandbox_root, rt):
    """**新会话的目录必须已经建好。**

    不建的话第一次 `ls` 得到的是「不存在」，模型据此会判定工作台不可用——而它只是空的。
    工具的返回值是模型唯一能看到的世界，「空」与「不存在」必须分得开。
    """
    assert _run(file_ops.ls, rt, path="/workspace") == "/workspace is empty."
    assert _run(file_ops.ls, rt, path="/input") == "/input is empty."
    assert _run(file_ops.ls, rt, path="/output") == "/output is empty."

    for name in ("input", "output", "workspace"):
        assert (_sandbox_root / "user" / "th-1" / name).is_dir()


def test_a_missing_subdirectory_still_says_it_does_not_exist(rt):
    """建的是**挂载根**，不是随便什么路径——`ls /workspace/nope` 不该凭空造目录。"""
    assert "does not exist" in _run(file_ops.ls, rt, path="/workspace/nope")


def test_skills_is_not_created_for_a_session(_sandbox_root, rt):
    """`/skills` 是全局的，不随会话建，也还没有生产者——如实说「不存在」比造个空目录好。"""
    _run(file_ops.ls, rt, path="/workspace")
    assert not (_sandbox_root / "skills-view").exists()


def test_ls_on_an_empty_directory(_sandbox_root, rt):
    (_sandbox_root / "user" / "th-1" / "output").mkdir(parents=True)
    assert "empty" in _run(file_ops.ls, rt, path="/output")


def test_glob_returns_virtual_paths(rt):
    _run(file_ops.write_file, rt, path="a/x.md", content="1")
    _run(file_ops.write_file, rt, path="b/y.txt", content="2")

    out = _run(file_ops.glob, rt, pattern="**/*.md")
    assert out.strip() == "/workspace/a/x.md"


def test_glob_rejects_traversal_patterns(rt):
    with pytest.raises(ValueError, match=r"\.\."):
        _run(file_ops.glob, rt, pattern="../*.txt")


def test_grep_finds_lines_with_file_and_line_number(rt):
    _run(file_ops.write_file, rt, path="s/a.md", content="keep\nneedle here\nkeep")
    _run(file_ops.write_file, rt, path="s/b.txt", content="needle too")

    out = _run(file_ops.grep, rt, pattern="needle", case_sensitive=True)
    assert "/workspace/s/a.md:2: needle here" in out
    assert "/workspace/s/b.txt:1: needle too" in out


def test_grep_literal_and_case_sensitivity(rt):
    _run(file_ops.write_file, rt, path="c.txt", content="a.b\naxb")

    assert "a.b" in _run(file_ops.grep, rt, pattern="a.b", literal=True)
    assert "No matches" in _run(file_ops.grep, rt, pattern="A\\.B", literal=True)


def test_grep_can_be_filtered_by_glob(rt):
    _run(file_ops.write_file, rt, path="f/a.md", content="token")
    _run(file_ops.write_file, rt, path="f/b.py", content="token")

    out = _run(file_ops.grep, rt, pattern="token", glob="**/*.py")
    assert "b.py" in out and "a.md" not in out


def test_grep_rejects_a_broken_regex(rt):
    with pytest.raises(ValueError, match="invalid regular expression"):
        _run(file_ops.grep, rt, pattern="([unclosed")


# --------------------------------------------------------------------------- #
# 权限与不泄漏
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tool_name", ["write_file", "str_replace"])
@pytest.mark.parametrize("path", ["/input/x.md", "/skills/y.md"])
def test_writes_into_read_only_mounts_are_refused(rt, tool_name, path):
    tool = getattr(file_ops, tool_name)
    with pytest.raises(PathNotAllowedError, match="只读"):
        _run(tool, rt, path=path, content="x", old_str="a", new_str="b")


def test_reads_from_input_are_allowed(_sandbox_root, rt):
    uploaded = _sandbox_root / "user" / "th-1" / "input" / "up.txt"
    uploaded.parent.mkdir(parents=True, exist_ok=True)
    uploaded.write_text("from the user", encoding="utf-8")

    assert "from the user" in _run(file_ops.read_file, rt, path="/input/up.txt")


def test_no_tool_output_leaks_the_host_root(_sandbox_root, rt):
    _run(file_ops.write_file, rt, path="a/b.txt", content="x")

    outputs = [
        _run(file_ops.ls, rt),
        _run(file_ops.glob, rt, pattern="**/*"),
        _run(file_ops.grep, rt, pattern="x"),
        _run(file_ops.read_file, rt, path="a/b.txt"),
    ]
    assert not any(str(_sandbox_root) in out for out in outputs), outputs


def test_a_traversal_attempt_is_refused_through_the_tool(rt):
    with pytest.raises(PathNotAllowedError):
        _run(file_ops.read_file, rt, path="../../.env")


# --------------------------------------------------------------------------- #
# 会话从运行上下文来
# --------------------------------------------------------------------------- #


def test_tools_use_the_session_from_a_lead_context(_sandbox_root):
    _run(file_ops.write_file, fake_runtime(WORKSPACE_CONTEXT), path="x.txt", content="1")
    assert (_sandbox_root / "user" / "th-1" / "workspace" / "x.txt").is_file()


def test_tools_use_the_session_from_a_coordinate_context(_sandbox_root):
    """坐标形状的上下文只给 ``{会话}:{运行}:{任务}``——取第一段，两个会话的文件不会混。"""
    _run(file_ops.write_file, fake_runtime(COORDINATE_CONTEXT), path="x.txt", content="1")
    assert (_sandbox_root / "user" / "th-1" / "workspace" / "x.txt").is_file()
