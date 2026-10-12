"""契约测试：``present_files`` 工具。

这一层钉的是**工具对模型与对事件流两条边**的承诺：

- 对**模型**：越界、不存在、是目录一律 ``raise``（不是返回 ``Error: ...`` 字符串），
  异常文案可操作且**不含宿主路径**。
- 对**事件流**：成功时送出一个 ``files_presented`` 载荷，路径一律虚拟口径——
  宿主绝对路径会一路漏到 SSE、journal 与浏览器。

所以每条「拒绝」用例都同时断言**没有送载荷**：失败却出了一张卡片，比失败本身更糟。

全部在 ``tmp_path`` 上跑，绝不碰真的 ``backend/.evaai``（夹具与 test_file_ops 同一手法）。
"""

import dataclasses
import json

import pytest
from tool_fakes import COORDINATE_CONTEXT, fake_runtime, lead_context

from app.tools import present_files as module
from evaharness.sandbox import PathNotAllowedError, SandboxLayout


@pytest.fixture
def layout(tmp_path):
    """这次运行的沙箱布局（根指向 tmp_path）——与 test_file_ops 同一手法。"""
    return SandboxLayout(root=tmp_path)


def _write(root, mount, relative, content="内容"):
    """在沙箱里造一个文件，返回它的宿主路径。"""
    target = root / "user" / "th-1" / mount / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _call(paths, sent, context):
    """调一次工具。*sent* 收集它送进 custom 流的载荷。"""
    runtime = dataclasses.replace(fake_runtime(context), stream_writer=sent.append)
    return module.present_files.invoke({"runtime": runtime, "paths": paths})


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #


def test_a_file_under_output_is_presented(layout):
    _write(layout.root, "output", "报告.md")

    sent: list = []
    result = _call(["/output/报告.md"], sent, lead_context(layout))

    assert "报告.md" in result, "返回值是给模型看的，得说清交了什么"
    assert len(sent) == 1, "一次调用只送一个载荷"
    assert sent[0]["kind"] == "files_presented"
    assert [f["path"] for f in sent[0]["files"]] == ["/output/报告.md"]


def test_the_payload_carries_name_size_and_mime(layout):
    """这三样是前端画卡片要的全部——少一样前端就得自己去猜或去问后端。"""
    _write(layout.root, "output", "a.md", "hello")

    sent: list = []
    _call(["/output/a.md"], sent, lead_context(layout))

    file = sent[0]["files"][0]
    assert file["name"] == "a.md"
    assert file["size"] == len(b"hello")
    assert file["mime"] == "text/markdown"


def test_several_files_go_out_in_one_payload(layout):
    """一次调用 = 一条事件 = 一张卡片列 N 个文件，而不是 N 张卡片。"""
    _write(layout.root, "output", "a.md")
    _write(layout.root, "output", "sub/b.go")

    sent: list = []
    _call(["/output/a.md", "/output/sub/b.go"], sent, lead_context(layout))

    assert len(sent) == 1
    assert [f["path"] for f in sent[0]["files"]] == ["/output/a.md", "/output/sub/b.go"]


def test_an_unknown_extension_leaves_mime_empty(layout):
    """``.go`` 之类认不出来就留空——编一个 ``application/octet-stream`` 出来只会让前端画错图标。"""
    _write(layout.root, "output", "quicksort.go", "package main")

    sent: list = []
    _call(["/output/quicksort.go"], sent, lead_context(layout))

    assert sent[0]["files"][0]["mime"] is None


def test_a_coordinate_shaped_context_also_works(layout):
    """会话 id 有两种上下文形状（``RunContext`` 对象 / ``{坐标}`` dict），工具都得认。"""
    _write(layout.root, "output", "a.md")

    sent: list = []
    _call(["/output/a.md"], sent, {**COORDINATE_CONTEXT, "sandbox": layout})

    assert sent[0]["files"][0]["path"] == "/output/a.md"


# --------------------------------------------------------------------------- #
# 宿主路径不外泄
# --------------------------------------------------------------------------- #


def test_the_payload_never_leaks_the_host_path(layout):
    """载荷会一路到 SSE、journal、浏览器——宿主路径漏出去就是信息泄漏 + 不可移植。"""
    _write(layout.root, "output", "a.md")

    sent: list = []
    _call(["/output/a.md"], sent, lead_context(layout))

    blob = json.dumps(sent, ensure_ascii=False)
    assert str(layout.root) not in blob
    assert ".evaai" not in blob


def test_a_refusal_does_not_leak_the_host_path(layout):
    """异常文案同样会进模型的上下文（中间件把它转成 ToolMessage），所以也要干净。"""
    sent: list = []
    with pytest.raises(ValueError) as excinfo:
        _call(["/output/nope.md"], sent, lead_context(layout))

    message = str(excinfo.value)
    assert "/output/nope.md" in message, "得让模型知道是哪个路径"
    assert str(layout.root) not in message


# --------------------------------------------------------------------------- #
# 拒绝（每条都同时断言「没有送载荷」）
# --------------------------------------------------------------------------- #


def test_a_workspace_file_is_refused(layout):
    """工作台是干活的地方，不是交付的地方——草稿不该出现在用户面前。"""
    _write(layout.root, "workspace", "draft.md")

    sent: list = []
    with pytest.raises(ValueError, match="/workspace/draft.md"):
        _call(["/workspace/draft.md"], sent, lead_context(layout))

    assert sent == [], "被拒绝的调用不该出卡片"


def test_a_relative_path_is_normalized_and_therefore_refused(layout):
    """相对路径落到 ``/workspace``，于是正好被挡——提示模型把交付物写全路径。"""
    _write(layout.root, "workspace", "draft.md")

    sent: list = []
    with pytest.raises(ValueError, match="/output"):
        _call(["draft.md"], sent, lead_context(layout))

    assert sent == []


def test_a_missing_file_is_refused(layout):
    sent: list = []
    with pytest.raises(ValueError, match="does not exist"):
        _call(["/output/never-written.md"], sent, lead_context(layout))

    assert sent == []


def test_a_directory_is_refused(layout):
    """``ls`` 能列目录，但交付物是文件——目录卡片点开来是没有意义的。"""
    _write(layout.root, "output", "sub/keep.md")

    sent: list = []
    with pytest.raises(ValueError, match="directory"):
        _call(["/output/sub"], sent, lead_context(layout))

    assert sent == []


def test_no_paths_is_refused(layout):
    """空调用会产一条空事件、画一张空卡片——那比报个错更难查。"""
    sent: list = []
    with pytest.raises(ValueError, match="no paths"):
        _call([], sent, lead_context(layout))

    assert sent == []


def test_a_traversal_path_is_refused_before_any_lookup(layout):
    """``..`` 在归一化那一层就被挡掉，根本走不到存在性检查。"""
    sent: list = []
    with pytest.raises(PathNotAllowedError):
        _call(["/output/../workspace/draft.md"], sent, lead_context(layout))

    assert sent == []


def test_an_absolute_host_path_is_refused(layout):
    """模型写出本机绝对路径时，也不能变成一次成功的呈现。"""
    sent: list = []
    with pytest.raises(PathNotAllowedError):
        _call(["C:/Windows/win.ini"], sent, lead_context(layout))

    assert sent == []
