"""技能：把 `SKILL.md` 读成 :class:`Skill`，再把一组技能渲染成提示词里的一段。

分三层，各自可单独测：

- :mod:`~evaharness.skills.parser` —— 读 `SKILL.md` 这个**文件格式**（纯函数，不碰磁盘）。
- :mod:`~evaharness.skills.prompt` —— 渲染成**给模型看的话**。

**文件系统那一半不在这里**：源目录 → `/skills` 只读视图的镜像投影在 `app/gateway/skills.py`，
因为「技能放哪、视图放哪」是策略与路径，不是机制。

**正文从不进提示词**（ADR-0018 决策 #2）：提示词里只有名字、描述和位置，模型需要时自己
`read_file` 去读全文。这样提示词随技能**个数**缓慢增长，而不是随正文体量膨胀；改一个技能也不会让
整个前缀缓存失效。
"""

from __future__ import annotations

from evaharness.skills.parser import (
    MAX_NAME_LENGTH,
    Skill,
    SkillError,
    is_valid_skill_name,
    parse_skill_md,
)
from evaharness.skills.prompt import render_skills_section

__all__ = [
    "MAX_NAME_LENGTH",
    "Skill",
    "SkillError",
    "is_valid_skill_name",
    "parse_skill_md",
    "render_skills_section",
]
