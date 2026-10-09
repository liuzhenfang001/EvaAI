"""投影：把仓库里的技能源目录**镜像**到 `/skills` 的只读视图。

全程用 `tmp_path` 当源与视图，**不碰真的 `backend/.evaai`**——与 `test_file_ops.py` 的
`_sandbox_root` fixture 同一条纪律：测试不该往运行期目录里写东西。

「镜像」是这里唯一的行为约定：源是唯一真相，**源里删掉的技能也要从视图里消失**，否则「从仓库移走」
永远不生效——那正是这类投影最容易留下的坑。
"""

from __future__ import annotations

import os
import pathlib

import pytest

from app.gateway.skills import project_skills
from evaharness.skills import Skill


def _write(
    root: pathlib.Path,
    name: str,
    frontmatter: str = "description: 帮我做事",
    body: str = "步骤一",
) -> pathlib.Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return directory


@pytest.fixture
def roots(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    return tmp_path / "skills", tmp_path / "view"


class TestProjection:
    def test_a_healthy_skill_lands_in_the_view(self, roots):
        src, view = roots
        _write(src, "writer", "name: writer\ndescription: 帮我写文档")

        skills = project_skills(src, view)

        assert skills == [Skill("writer", "帮我写文档", "/skills/writer/SKILL.md")]
        assert (view / "writer" / "SKILL.md").is_file()

    def test_the_location_is_a_virtual_path_not_a_host_path(self, roots):
        """宿主路径永远不进模型上下文（ADR-0012）——位置必须是 `/skills/...`。"""
        src, view = roots
        _write(src, "writer", "name: writer\ndescription: d")

        [skill] = project_skills(src, view)

        assert skill.location.startswith("/skills/")
        assert str(src) not in skill.location

    def test_attachments_come_along(self, roots):
        src, view = roots
        directory = _write(src, "writer", "name: writer\ndescription: d")
        (directory / "templates").mkdir()
        (directory / "templates" / "周报.md").write_text("模板正文", encoding="utf-8")

        project_skills(src, view)

        assert (view / "writer" / "templates" / "周报.md").read_text(encoding="utf-8") == "模板正文"

    def test_the_source_is_copied_not_moved(self, roots):
        src, view = roots
        _write(src, "writer", "name: writer\ndescription: d")

        project_skills(src, view)

        assert (src / "writer" / "SKILL.md").is_file(), "源被搬走了——它是唯一真相，必须原地不动"

    def test_a_skill_removed_from_the_source_disappears_from_the_view(self, roots):
        """镜像的核心：源里没了，视图里也不能留。"""
        src, view = roots
        _write(src, "writer", "name: writer\ndescription: d")
        project_skills(src, view)
        assert (view / "writer").exists()

        (src / "writer" / "SKILL.md").unlink()
        (src / "writer").rmdir()
        skills = project_skills(src, view)

        assert skills == []
        assert not (view / "writer").exists()


class TestSkipping:
    def test_a_directory_without_skill_md_is_skipped(self, roots, caplog):
        src, view = roots
        (src / "notaskill").mkdir(parents=True)

        assert project_skills(src, view) == []
        assert "SKILL.md" in caplog.text

    def test_a_broken_skill_is_reported_and_skipped(self, roots, caplog):
        """决议 #5：**响亮地跳过**——不拒绝启动，但也不静默。

        「响亮」的判据是警告里能看出**是哪个文件**、**差在哪里**。
        """
        src, view = roots
        _write(src, "good", "name: good\ndescription: d")
        _write(src, "bad", "name: bad")  # 缺 description

        skills = project_skills(src, view)

        assert [skill.name for skill in skills] == ["good"]
        assert not (view / "bad").exists(), "坏技能不该进视图——模型看到的目录必须只有能用的"
        assert "bad" in caplog.text and "description" in caplog.text

    def test_a_broken_skill_never_raises(self, roots):
        """一个写坏的技能不能拦住启动，也不能拦住别的技能。"""
        src, view = roots
        _write(src, "bad", "name: bad")  # 缺 description
        (src / "broken").mkdir()
        (src / "broken" / "SKILL.md").write_text("完全没有 frontmatter", encoding="utf-8")

        assert project_skills(src, view) == []

    def test_an_invalid_directory_name_is_skipped(self, roots, caplog):
        src, view = roots
        _write(src, "a..b", "name: a..b\ndescription: d")

        assert project_skills(src, view) == []
        assert "a..b" in caplog.text


class TestTheViewDirectoryItself:
    def test_a_missing_source_directory_is_not_an_error(self, roots):
        src, view = roots
        assert not src.exists()

        assert project_skills(src, view) == []
        assert not view.exists(), "没有技能就不该造目录（ADR-0012：如实报不存在比造个空目录好）"

    def test_the_view_is_removed_once_the_last_skill_is_gone(self, roots):
        src, view = roots
        _write(src, "writer", "name: writer\ndescription: d")
        project_skills(src, view)

        (src / "writer" / "SKILL.md").unlink()
        (src / "writer").rmdir()
        project_skills(src, view)

        assert not view.exists(), "最后一个技能走了，空视图也该走——否则模型会看到一个空的技能库"

    def test_a_stale_entry_left_in_the_view_is_cleaned_up(self, roots):
        src, view = roots
        _write(src, "writer", "name: writer\ndescription: d")
        stale = view / "stale-skill"
        stale.mkdir(parents=True)
        (stale / "SKILL.md").write_text("旧东西", encoding="utf-8")

        project_skills(src, view)

        assert not stale.exists()

    def test_nothing_outside_the_view_is_touched(self, roots, tmp_path):
        """删除前的守卫：算错一个前缀就会删掉 `/skills` 之外的文件，而那是不可逆的。"""
        src, view = roots
        _write(src, "writer", "name: writer\ndescription: d")
        decoy = tmp_path / "decoy"
        decoy.mkdir()
        (decoy / "重要文件").write_text("留着", encoding="utf-8")

        project_skills(src, view)

        assert (decoy / "重要文件").read_text(encoding="utf-8") == "留着"


class TestSymlinks:
    def test_a_symlinked_skill_directory_is_not_followed(self, roots, caplog):
        """不跟符号链接/junction：Windows 上建 junction 连管理员都不需要，是现成的逃逸手法。"""
        src, view = roots
        src.mkdir(parents=True)
        real = _write(src, "real", "name: real\ndescription: d")
        link = src / "link"
        try:
            os.symlink(real, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("这个环境建不了符号链接（Windows 默认要特权）")

        assert [skill.name for skill in project_skills(src, view)] == ["real"]
