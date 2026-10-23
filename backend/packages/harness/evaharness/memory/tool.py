"""模型手里那三件记忆工具：记一条、查一条、删一条。

**这是「主动写」那一半**——模型自己判断「这件事值得长期记住」。另一半（每轮结束后调 LLM 从对话
里抽事实与更新画像）是 T21 的事，两者写同一张表。

工具实例**跨请求共享**（ADR-0011），所以它们在构造期把 ``store`` 绑进闭包，自己**不持有任何
请求级状态**——唯一的请求级信息（这条事实来自哪个会话）从运行上下文里现取。这与
:func:`evaharness.knowledge.tool.make_knowledge_search` 同一个形状。

**三个都写成 ``async def``，这不是风格而是必需**：同步工具会被 langgraph 的 ToolNode 丢进
**工作线程**执行，而 ``store`` 的 SQLite 连接是在事件循环线程上建的——跨线程用会撞 sqlite 的
线程亲和性检查。改成协程后它们留在事件循环线程上，「建连接」与「用连接」是同一个线程。

**预期之内的否定结果返回字符串，不抛异常**：重复记一条、按 id 找不到、分类写错——这些都不是
「工具坏了」，是模型该读到并据此改主意的信息。异常路径留给真正出乎意料的事（存储挂了），
由 ``ToolErrorHandlingMiddleware`` 兜成 error ToolMessage。
"""

from __future__ import annotations

from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool

from evaharness.memory.schema import DEFAULT_FACT_CATEGORY, DEFAULT_FACT_CONFIDENCE, FACT_CATEGORIES
from evaharness.memory.store import DuplicateFact, MemoryStore
from evaharness.runtime import session_id

MEMORY_SAVE_NAME = "memory_save"
MEMORY_SEARCH_NAME = "memory_search"
MEMORY_FORGET_NAME = "memory_forget"

MEMORY_TOOL_NAMES = frozenset({MEMORY_SAVE_NAME, MEMORY_SEARCH_NAME, MEMORY_FORGET_NAME})
"""三件记忆工具的名字集合（用于「只属于 lead」的剔除）。"""

LEAD_MEMORY_TOOL_NAMES = frozenset({MEMORY_SEARCH_NAME})
"""**真正交给模型的那一件**。

T21 起写入完全被动：``memory_save`` / ``memory_forget`` 保留在代码里（管理页、调试与将来的
tool 模式都要用），但**不进 lead 的工具表**——模型主动记东西会多占一步主线任务，还会让用户
在任务面板里看到「助手在操作记忆」。模型只需要在需要旧事实时能查。
"""

_CATEGORY_HELP = ", ".join(sorted(FACT_CATEGORIES))


def make_memory_tools(store: MemoryStore) -> list[BaseTool]:
    """建那三个工具。``store`` 是**启动期常量**，在构造期绑进闭包（ADR-0012 §2）。"""

    @tool(MEMORY_SAVE_NAME, parse_docstring=True)
    async def memory_save(
        runtime: ToolRuntime[Any],
        content: str,
        category: str = DEFAULT_FACT_CATEGORY,
        confidence: float = DEFAULT_FACT_CONFIDENCE,
    ) -> str:
        """Save one durable fact about the user, to be remembered in future conversations.

        Use it for things that stay true beyond this conversation: who the user is, how they
        want to be answered, corrections they have made, ongoing goals and constraints. Do
        NOT use it for one-off task details, file paths, or anything you would not want to
        see again next month.

        Args:
            content: The fact, written as one short self-contained sentence in the user's
                language. Write it so it stands alone without this conversation.
            category: One of: preference, correction, identity, goal, constraint, decision,
                behavior, context, other. Use "correction" when the user told you that
                something you did or assumed was wrong.
            confidence: How sure you are that this is true and durable, from 0 to 1.
        """
        if category not in FACT_CATEGORIES:
            return f"Unknown category {category!r}. Pick one of: {_CATEGORY_HELP}."
        try:
            fact = store.add_fact(
                content=content,
                category=category,
                confidence=confidence,
                source_thread=session_id(runtime),
            )
        except DuplicateFact:
            return f"Already remembered, nothing to do: {content.strip()}"
        except ValueError as exc:
            return f"Could not save that: {exc}"
        return f"Remembered as {fact.id} ({fact.category})."

    @tool(MEMORY_SEARCH_NAME, parse_docstring=True)
    async def memory_search(query: str = "", category: str = "", since: str = "", limit: int = 10) -> str:
        """Look up what you already remember about the user.

        Results include each memory's id — keep it if you may need to forget or correct that
        memory later.

        Args:
            query: Text to look for, matched against what was stored. May be empty when
                ``since`` is given (that asks "what changed since then?").
            category: Optional filter, one of the categories memory_save accepts.
            since: Optional start time in ISO 8601 (e.g. "2026-10-01" or
                "2026-10-01T12:00:00Z"). Only memories updated at or after it are returned.
            limit: Maximum number of memories to return.
        """
        if category and category not in FACT_CATEGORIES:
            return f"Unknown category {category!r}. Pick one of: {_CATEGORY_HELP}."
        try:
            facts = store.search_facts(
                query,
                category=category or None,
                since=since or None,
                limit=limit,
            )
        except ValueError as exc:
            return f"Could not search that: {exc}"
        if not facts:
            return "Nothing in long-term memory matches that."
        noun = "memory" if len(facts) == 1 else "memories"
        lines = [f"- {fact.id} [{fact.category} | {fact.confidence:.2f}] {fact.content}" for fact in facts]
        return f"{len(facts)} {noun}:\n" + "\n".join(lines)

    @tool(MEMORY_FORGET_NAME, parse_docstring=True)
    async def memory_forget(fact_id: str) -> str:
        """Delete one memory by its id, when the user asks you to stop remembering something.

        Args:
            fact_id: The id shown by memory_search (looks like "fact_9f3c1a2b4d5e").
        """
        if store.forget_fact(fact_id):
            return f"Forgot {fact_id}."
        return f"No memory with id {fact_id}."

    return [memory_save, memory_search, memory_forget]


__all__ = [
    "LEAD_MEMORY_TOOL_NAMES",
    "MEMORY_FORGET_NAME",
    "MEMORY_SAVE_NAME",
    "MEMORY_SEARCH_NAME",
    "MEMORY_TOOL_NAMES",
    "make_memory_tools",
]
