"""`SKILL.md` 这个**文件格式**：把一份文本读成 :class:`Skill`。

**这一层是纯的**——不读文件、不遍历目录、不碰配置。文件系统那一半（源目录 → 只读视图的投影）在
`app/gateway/skills.py`，因为「技能放哪、视图放哪」是策略与路径，不是机制。

**正文为什么不在这里**：技能正文**不进提示词**（ADR-0018 决策 #2）。这个类只带 `name` /
`description` / `location`，模型需要时自己 `read_file` 去读全文。理由是让 system prompt 保持静态
——它随**技能个数**缓慢增长，而不是随技能正文的体量膨胀；而且改一个技能的正文不必让整个前缀缓存
失效。

**技能名允许中文**（决策 #9）：本项目提示词全是中文，禁中文会很意外。禁的是会造成歧义或越界的
东西——路径分隔符、`..`、控制字符、空白、首尾的点。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml

MAX_NAME_LENGTH = 64
"""技能名的长度上限。够长，又短到能安全地当路径段。"""

_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_FORBIDDEN_IN_NAME = frozenset("/\\")


@dataclass(frozen=True, slots=True)
class Skill:
    """一个技能。**没有正文**——正文由模型自己去读，见模块说明。"""

    name: str
    """技能的标识，也是它在虚拟路径里的目录名（``/skills/<name>/``）。**权威来源是目录名。**"""

    description: str
    """一句话说清「什么时候该用它」。它直接进提示词，所以模型只凭这一句决定要不要读正文。"""

    location: str
    """`SKILL.md` 的**虚拟路径**（``/skills/<name>/SKILL.md``）。

    调用方构造——harness 不该知道宿主目录，宿主路径也永远不进模型上下文（ADR-0012）。
    """


class SkillError(ValueError):
    """技能文件不合法。**只用于单个技能**——调用方据此跳过它并记一条响亮的警告。

    继承 ``ValueError``：调用方按语义捕获「这个技能写得不对」时不必知道我们的类名。
    """


def is_valid_skill_name(name: str) -> bool:
    """这个名字能不能当技能目录名。

    禁的东西各有理由，不是洁癖：

    - ``/`` 与 ``\\``：它要当**路径段**用，含分隔符就换了一层目录。
    - ``..``：路径段里唯一能往上跳的东西（``a..b`` 也一并禁——没人真的这么命名，而规则越简单
      越好记）。
    - 控制字符与**空白**：它会出现在提示词的路径里，空白让那段路径有歧义。
    - 首尾的点：``.hidden`` 在类 Unix 上是隐藏目录，``trailing.`` 在 Windows 上会被系统悄悄去掉
      ——两种都会让「目录名」与「技能名」在某个平台上不再相等。
    """

    if not name or len(name) > MAX_NAME_LENGTH:
        return False
    if name in (".", "..") or ".." in name:
        return False
    if name.startswith(".") or name.endswith("."):
        return False
    if any(char in _FORBIDDEN_IN_NAME for char in name):
        return False
    if any(char.isspace() for char in name):
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        return False
    return True


def parse_skill_md(text: str, *, name: str, location: str) -> Skill:
    """把一份 `SKILL.md` 的文本解析成 :class:`Skill`。

    ``name`` 是**目录名**（权威），``location`` 是那份文件对模型可见的虚拟路径——两者都由调用方给，
    所以这个函数不需要知道磁盘布局。

    Raises:
        SkillError: 缺 frontmatter、YAML 坏、不是键值、缺 ``name`` / ``description``、
            声明名与目录名不一致、或目录名本身不合法。消息里带 ``location``，所以跳过是**可查的**
            （决策 #5：响亮地跳过，不静默）。
    """

    def fail(reason: str) -> SkillError:
        return SkillError(f"{location} 不是有效的技能：{reason}")

    if not is_valid_skill_name(name):
        raise fail(f"技能名 {name!r} 不合法（不能含 / \\ .. 空白或控制字符，也不能以点开头结尾）")

    match = _FRONTMATTER.match(text)
    if match is None:
        raise fail("开头没有 YAML frontmatter（第一行必须是 `---`，且要有成对的结束 `---`）")

    try:
        data: Any = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise fail(f"frontmatter 的 YAML 解析失败：{exc}") from None

    if not isinstance(data, dict):
        raise fail("frontmatter 必须是一组键值（`key: value`），实际不是")

    declared = data.get("name")
    if not isinstance(declared, str) or not declared.strip():
        raise fail("frontmatter 里缺 `name`（或它是空的）")
    if declared.strip() != name:
        # 目录名才是权威：它决定虚拟路径，也决定技能之间怎么区分。
        raise fail(f"frontmatter 的 name={declared.strip()!r} 与目录名 {name!r} 不一致——目录名才是权威")

    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise fail("frontmatter 里缺 `description`（或它是空的）")

    # 其余键一律忽略：真实技能会带 `license` / `version` / `allowed-tools` 这些我们不做的东西，
    # 拒绝它们等于把能用的技能误杀（决策 #4 明确不做 allowed-tools，但也不该因此报错）。
    return Skill(name=name, description=description.strip(), location=location)


__all__ = ["MAX_NAME_LENGTH", "Skill", "SkillError", "is_valid_skill_name", "parse_skill_md"]
