"""长期记忆的数据形状：六块画像 + 原子事实。

**记忆不是会话**。会话记忆在 checkpointer 里（``{会话}:lead`` 那条固定坐标，ADR-0007 /
0009），随会话生灭；这里定义的是**跨会话**留下来的两样东西：

- **画像（profile）**：六块叙述性摘要，回答「这个人是谁、最近在忙什么」。分两组——
  ``user``（工作 / 个人 / 当前关注）与 ``history``（最近几个月 / 更早的上下文 / 长期背景）。
- **事实（fact）**：一条条可增删改的原子断语（偏好、纠正、身份、目标、约束……），带置信度与
  来源会话，可回溯。

两者**与 `SummarizationMiddleware` 不重叠**：那个压的是本 thread 的上下文（会话内、按 token
触发），画像里的 history 那三块是**跨会话**的长期历史记录。也与知识库不重叠——那是仓库文本的
向量索引，是「查资料」，这里是「记住你」。

参照项目 DeerFlow 把画像存 manifest JSON、每条事实存一个 Markdown 文件（sha256 分片 + 乐观锁）。
我们**单用户、单进程**，用一张 SQLite 表就够——那套分片与乐观锁是为多用户与跨进程写写的，
形状留着（字段名对齐），机制不抄。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4


class ProfileSection(StrEnum):
    """画像的六块。取值即存进库里的 ``memory_profile.section``。"""

    WORK_CONTEXT = "work_context"
    PERSONAL_CONTEXT = "personal_context"
    TOP_OF_MIND = "top_of_mind"
    RECENT_MONTHS = "recent_months"
    EARLIER_CONTEXT = "earlier_context"
    LONG_TERM_BACKGROUND = "long_term_background"


PROFILE_GROUPS: dict[str, tuple[ProfileSection, ...]] = {
    "user": (ProfileSection.WORK_CONTEXT, ProfileSection.PERSONAL_CONTEXT, ProfileSection.TOP_OF_MIND),
    "history": (ProfileSection.RECENT_MONTHS, ProfileSection.EARLIER_CONTEXT, ProfileSection.LONG_TERM_BACKGROUND),
}
"""两块的分组。**顺序即渲染顺序**，也是「哪些块还没写过」的检查清单。"""

FACT_CATEGORIES = frozenset(
    {
        "preference",
        "correction",
        "identity",
        "goal",
        "constraint",
        "decision",
        "behavior",
        "context",
        "other",
    }
)
"""事实的分类。``correction``（用户纠正过的事）在注入时享有一条保底通道——记错了会再犯。"""

DEFAULT_FACT_CATEGORY = "context"
"""没把握时的兜底分类。``context`` 是「关于处境的事实」，不承诺任何偏好或规则。"""

DEFAULT_FACT_CONFIDENCE = 0.7
"""模型没给置信度时的缺省。与参照项目 ``fact_confidence_threshold`` 同值。"""


def new_fact_id() -> str:
    """一条新事实的 id，形如 ``fact_9f3c1a2b4d5e``。"""
    return f"fact_{uuid4().hex[:12]}"


@dataclass(frozen=True)
class Fact:
    """一条原子事实。"""

    content: str
    category: str = DEFAULT_FACT_CATEGORY
    confidence: float = DEFAULT_FACT_CONFIDENCE
    id: str = ""
    status: str = "active"
    """``active`` / ``superseded``。目前只有前者的生产者（本片与 T21 都不写后者）。"""
    source_thread: str | None = None
    """这条事实是哪次会话产生的。**只是可追溯的来源，不是分区键**——单用户部署下记忆全局一份。"""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ProfileBlock:
    """画像里的一块。``section`` 是 :class:`ProfileSection` 的值。"""

    section: str
    summary: str
    updated_at: str = ""


@dataclass(frozen=True)
class MemorySnapshot:
    """一次读出的全部记忆：注入、管理页、抽取（T21）都吃这一个形状。

    ``facts`` 为 :data:`None` 时按空元组处理是不允许的——造快照的人自己决定空不空，
    这里不做兜底转换（那会让「读失败了」和「确实没有」长得一样）。
    """

    profile: tuple[ProfileBlock, ...] = ()
    facts: tuple[Fact, ...] = field(default_factory=tuple)

    @property
    def empty(self) -> bool:
        """两样都没有。**空快照是「不注入任何东西」的唯一依据**（见 :func:`render_memory_section`）。"""
        return not self.profile and not self.facts


__all__ = [
    "DEFAULT_FACT_CATEGORY",
    "DEFAULT_FACT_CONFIDENCE",
    "FACT_CATEGORIES",
    "PROFILE_GROUPS",
    "Fact",
    "MemorySnapshot",
    "ProfileBlock",
    "ProfileSection",
    "new_fact_id",
]
