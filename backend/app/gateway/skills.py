"""把仓库里的技能源目录**镜像**到 `/skills` 的只读视图。

`/skills` 这个挂载点从 T4 起就建好了、一直只读，但**没有生产者**——ADR-0012 当年写的是「全局共享的，
不随会话建，也还没有生产者」。本模块就是那个生产者。

**为什么是投影，而不是直接把源目录挂上去**：源在仓库里（人工维护、进版本控制、可 review），视图在
`backend/.evaai/` 下（运行期数据、已 gitignore）。分成两层之后，模型的写入面仍然只有那一个只读挂载，
而「技能从哪来」以后可以换（比如从别处取）而不动沙箱那一层。

**投影是镜像**：源是唯一真相，源里删掉的技能要从视图里一并消失——否则「从仓库移走」永远不生效，
那是这类同步最容易留下的坑（第一次投影之后，视图就开始自己长东西）。

单条失败一律**跳过并记警告**，永不抛（决议 #5）：技能是**可选内容**，不是 agent 的能力边界
（工具才是，所以 `config.yaml` 的工具写错就拒绝启动）。这是刻意偏离参照项目的——它解析失败时静默
返回 `None`，而静默失效正是本项目一贯拒绝的失败形状。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from evaharness.skills import Skill, SkillError, parse_skill_md

logger = logging.getLogger(__name__)

SKILL_FILE = "SKILL.md"
"""技能目录里的主文件名。有它才算一个技能包。"""


def project_skills(src_root: Path, view_root: Path) -> list[Skill]:
    """把 `src_root` 下的技能镜像到 `view_root`，返回解析成功的那些。

    **只接两个显式 `Path`、不读任何模块全局**——缝就在这里，所以测试不必 monkeypatch 谁，
    生产代码也只是把两个根拼出来传进来。

    没有任何技能时**不创建** `view_root`；上一个技能被移走后，空掉的视图目录也会被删掉
    ——兑现 ADR-0012 那句「它如实报『不存在』比造个空目录好」。模型看到的目录里只有真能用的技能。
    """
    skills, valid = _collect(src_root)
    _mirror(src_root, view_root, valid)
    return skills


def _collect(src_root: Path) -> tuple[list[Skill], set[str]]:
    """扫源目录的一级子目录，逐个解析；坏的点名跳过。"""
    skills: list[Skill] = []
    valid: set[str] = set()
    if not src_root.is_dir():
        return skills, valid

    for entry in sorted(src_root.iterdir()):
        if _is_link(entry) or not entry.is_dir():
            continue
        name = entry.name
        try:
            text = (entry / SKILL_FILE).read_text(encoding="utf-8")
            skill = parse_skill_md(text, name=name, location=f"/skills/{name}/{SKILL_FILE}")
        except FileNotFoundError:
            logger.warning("技能已跳过：%s 里没有 %s", entry, SKILL_FILE)
            continue
        except (SkillError, OSError, UnicodeDecodeError) as exc:
            # SkillError 之外还有读盘失败与编码失败——它们同样只是「这个技能用不了」，
            # 不该升级成启动失败。
            logger.warning("技能已跳过：%s（%s）", entry, exc)
            continue
        skills.append(skill)
        valid.add(name)
    return skills, valid


def _mirror(src_root: Path, view_root: Path, valid: set[str]) -> None:
    """把有效技能拷进视图，并清掉视图里源已经没有的残留。"""
    if valid:
        try:
            view_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("技能视图目录建不出来，本次不会有技能：%s（%s）", view_root, exc)
            return

    for name in sorted(valid):
        target = view_root / name
        # 先删再拷：源里删掉的**附件**也必须跟着消失，否则视图会慢慢长出源里没有的东西。
        if not _remove(target, view_root):
            continue
        try:
            shutil.copytree(src_root / name, target, symlinks=True, ignore_dangling_symlinks=True)
        except OSError as exc:
            logger.warning("技能 %r 拷贝到视图失败，它这次不会有：%s", name, exc)

    _prune(view_root, valid)


def _prune(view_root: Path, valid: set[str]) -> None:
    """清掉视图里不在有效集合中的直接子项；清空之后把视图目录本身也去掉。"""
    if not view_root.is_dir():
        return
    for entry in sorted(view_root.iterdir()):
        if entry.name not in valid:
            _remove(entry, view_root)
    try:
        if not any(view_root.iterdir()):
            view_root.rmdir()
            logger.info("没有技能，已移除视图目录：%s", view_root)
    except OSError as exc:
        logger.warning("空技能视图删不掉：%s（%s）", view_root, exc)


def _remove(path: Path, view_root: Path) -> bool:
    """删掉视图里的一个直接子项；**不在视图内的一律不碰**。

    守卫的理由很实在：这是**不可逆**操作，一个算错的前缀就会删掉 `/skills` 之外的东西。
    正常情况下这一关永远为真（路径来自 `iterdir()` 或校验过的技能名），留着是因为它代价极低
    而后果极重——与 :func:`app.tools.sandbox._is_within` 同一个顾虑。
    """
    if not path.exists() and not path.is_symlink():
        return True
    try:
        inside = path.parent.resolve() == view_root.resolve()
    except OSError:
        inside = False
    if not inside:
        logger.warning("拒绝删除技能视图之外的路径：%s", path)
        return False

    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        logger.warning("删不掉技能视图里的 %s：%s", path, exc)
        return False
    return True


def _is_link(path: Path) -> bool:
    """符号链接或 Windows junction。

    不跟随它们：junction 在 Windows 上**连管理员权限都不需要**就能建，而它是现成的逃逸手法
    ——`:mod:`app.tools.sandbox` 在解析后还要再比一次包含性，正是同一个顾虑。
    """
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


__all__ = ["SKILL_FILE", "project_skills"]
