"""契约测试：文件沙箱。

这是本片的安全关键面。**每一条越界都必须被拒**，而拒绝的方式还要满足两条产品约束：
错误消息里不能出现宿主路径（泄漏 + 不可移植），并且必须告诉模型「允许哪些前缀」——
模型拿别人家的路径来试是常态。
"""

import subprocess

import pytest
from tool_fakes import COORDINATE_CONTEXT, WORKSPACE_CONTEXT, fake_runtime

from app.tools.sandbox import MOUNTS, PathNotAllowedError, Sandbox
from evaharness.agents.middlewares.context import session_id


@pytest.fixture
def box(tmp_path) -> Sandbox:
    """注入宿主根，绝不碰真的 ``backend/.evaai``。"""
    return Sandbox("th-1", root=tmp_path)


# --------------------------------------------------------------------------- #
# 挂载点
# --------------------------------------------------------------------------- #


def test_relative_path_lands_in_workspace(box, tmp_path):
    """相对路径是主路径（模型少写一截），落点必须是工作台。"""
    assert box.resolve("notes/todo.md") == (tmp_path / "user" / "th-1" / "workspace" / "notes" / "todo.md")


@pytest.mark.parametrize(
    ("virtual", "parts"),
    [
        ("/workspace/a.txt", ("user", "th-1", "workspace", "a.txt")),
        ("/output/report.md", ("user", "th-1", "output", "report.md")),
        ("/input/upload.pdf", ("user", "th-1", "input", "upload.pdf")),
        ("/skills/writer/SKILL.md", ("skills-view", "writer", "SKILL.md")),
    ],
)
def test_each_mount_maps_under_its_own_host_dir(box, tmp_path, virtual, parts):
    assert box.resolve(virtual) == tmp_path.joinpath(*parts)


def test_skills_is_global_while_the_others_are_per_session(tmp_path):
    """skills 是共享的，input/output/workspace 按会话分——这是「换会话不串文件」的全部依据。"""
    assert Sandbox("th-1", root=tmp_path).resolve("/skills/a") == Sandbox("th-2", root=tmp_path).resolve("/skills/a")
    assert Sandbox("th-1", root=tmp_path).resolve("/workspace/a") != Sandbox("th-2", root=tmp_path).resolve(
        "/workspace/a"
    )


def test_every_mount_is_reachable(box):
    assert {mount.virtual for mount in MOUNTS} == {"/input", "/output", "/workspace", "/skills"}


# --------------------------------------------------------------------------- #
# 权限
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("virtual", ["/input/x.txt", "/skills/x.md"])
def test_read_only_mounts_refuse_writes(box, virtual):
    with pytest.raises(PathNotAllowedError, match="只读"):
        box.resolve(virtual, write=True)


def test_read_only_mounts_still_read(box):
    assert box.resolve("/input/x.txt").name == "x.txt"
    assert box.resolve("/skills/x.md").name == "x.md"


@pytest.mark.parametrize("virtual", ["/workspace/x.txt", "/output/x.txt"])
def test_writable_mounts_allow_writes(box, virtual):
    assert box.resolve(virtual, write=True).name == "x.txt"


# --------------------------------------------------------------------------- #
# 越界
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "virtual",
    [
        "/workspace/../input/x.txt",
        "/workspace/../../.env",
        "..\\..\\Windows\\win.ini",
        "../secrets.txt",
        "/../etc/passwd",
    ],
)
def test_dotdot_segments_are_rejected(box, virtual):
    with pytest.raises(PathNotAllowedError, match=r"\.\."):
        box.resolve(virtual)


@pytest.mark.parametrize("virtual", ["C:/Windows/win.ini", "C:\\Windows\\win.ini", "d:file.txt"])
def test_host_absolute_paths_are_rejected(box, virtual):
    with pytest.raises(PathNotAllowedError):
        box.resolve(virtual)


@pytest.mark.parametrize("virtual", ["~/.ssh/id_rsa", "//server/share/x", "\\\\server\\share"])
def test_tilde_and_network_paths_are_rejected(box, virtual):
    with pytest.raises(PathNotAllowedError):
        box.resolve(virtual)


@pytest.mark.parametrize("virtual", ["/etc/passwd", "/mnt/user-data/workspace/x", "/", "/User/th-1"])
def test_paths_outside_the_mount_table_are_rejected(box, virtual):
    with pytest.raises(PathNotAllowedError):
        box.resolve(virtual)


def test_a_dotdot_inside_a_filename_is_fine(box):
    """只拦**整段**等于 ``..`` 的——`a..b.txt` 是合法文件名，用 `".." in path` 会误伤。"""
    assert box.resolve("/workspace/a..b.txt").name == "a..b.txt"


def test_a_nul_byte_is_rejected(box):
    with pytest.raises(PathNotAllowedError):
        box.resolve("/workspace/a\x00b")


def test_an_empty_path_means_the_workspace(box):
    assert box.resolve("") == box.resolve("/workspace")


def _link_directory(link, target) -> bool:
    """在 workspace 里造一个指向外面的目录链接，成功返回 True。

    先试符号链接；Windows 上它要开发者模式，拿不到就退到 **junction**——junction 不需要
    任何特权，而且 ``Path.resolve()`` 一样会跟进去。两条都失败才跳过。
    """
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    result = subprocess.run(  # noqa: S603
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        errors="replace",
    )
    return result.returncode == 0


def test_a_link_escape_is_denied(box, tmp_path):
    """**词法层看不出问题的那一类**：workspace 里放一个指向外面的链接。

    ``_normalize`` 只看字符串，``/workspace/escape/loot.txt`` 完全合法；挡住它的是
    ``_beneath`` 里「解析之后再比一次包含性」那一层。这条测试就是那一层的证词。
    """
    outside = tmp_path.parent / "evaai-outside"
    outside.mkdir(exist_ok=True)
    (outside / "loot.txt").write_text("secret", encoding="utf-8")

    host_workspace = box.resolve("/workspace", write=True)
    host_workspace.mkdir(parents=True, exist_ok=True)
    if not _link_directory(host_workspace / "escape", outside):
        pytest.skip("本机既不支持符号链接也不支持 junction")

    with pytest.raises(PathNotAllowedError):
        box.resolve("/workspace/escape/loot.txt")


# --------------------------------------------------------------------------- #
# 不泄漏宿主路径
# --------------------------------------------------------------------------- #


def test_errors_never_leak_the_host_root(box, tmp_path):
    with pytest.raises(PathNotAllowedError) as exc:
        box.resolve("/etc/passwd")
    assert str(tmp_path.resolve()) not in str(exc.value)


def test_display_maps_back_to_virtual_paths(box):
    assert box.display(box.resolve("/workspace/notes/a.md")) == "/workspace/notes/a.md"
    assert box.display(box.resolve("/skills")) == "/skills"


def test_mask_redacts_the_host_root(box, tmp_path):
    masked = box.mask(f"boom at {tmp_path.resolve()}/user/th-1/workspace/a.txt")
    assert str(tmp_path.resolve()) not in masked
    assert "<evaai>" in masked


def test_contains_only_accepts_paths_under_a_mount(box, tmp_path):
    assert box.contains(box.resolve("/workspace/a.txt"))
    assert not box.contains(tmp_path.parent / "elsewhere" / "a.txt")


# --------------------------------------------------------------------------- #
# 会话 id
# --------------------------------------------------------------------------- #


def test_a_bad_session_id_is_rejected(tmp_path):
    """会话 id 会当路径段用——``..`` 与 ``a/b`` 能直接毁掉挂载根。"""
    for bad in ("..", "a/b", "", "../../etc", "a b", "x" * 65):
        with pytest.raises(PathNotAllowedError):
            Sandbox(bad, root=tmp_path)


def test_session_id_from_a_lead_context():
    assert session_id(fake_runtime(WORKSPACE_CONTEXT)) == "th-1"


def test_session_id_from_a_coordinate_context():
    """坐标形状只有 ``{会话}:{运行}:{任务}``，取第一段——两个会话的文件绝不能混。"""
    assert session_id(fake_runtime(COORDINATE_CONTEXT)) == "th-1"


def test_session_id_without_a_context_fails_loudly():
    """取不到就抛：兜到某个默认目录会把不同会话的文件混在一起，而且不可逆。"""
    with pytest.raises(ValueError):
        session_id(fake_runtime(None))
