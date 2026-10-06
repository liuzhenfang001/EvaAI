"""按配置装配工具：**「agent 能做什么」的唯一来源**。

放在业务层而不是 harness：有哪些工具、叫什么名字、开放哪些参数，都是业务决定；harness
只提供 :func:`~evaharness.reflection.resolve_variable` 这个机制。

加一个工具的完整动作 = 写一个 ``@tool``（或一个返回工具的工厂）+ 在 ``config.yaml`` 的
``tools:`` 里加一项。全程不改 Python 框架代码。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.tools import BaseTool

from evaharness.agents.lead_agent import TASK_TOOL_NAME
from evaharness.config.app_config import AppConfig, ToolConfig
from evaharness.reflection import ReflectionError, resolve_variable

_EMPTY_HINT = (
    "config.yaml 的 `tools:` 是空的。工具是 agent 的能力边界，所以这里没有隐藏的默认集——"
    "把 config.example.yaml 里的 `tools:` 段复制过去即可。"
)


def build_tools(app_config: AppConfig) -> list[BaseTool]:
    """把配置里的工具项建出来。**启动期调一次**，任何一项有问题就拒绝启动。

    Raises:
        ValueError: 工具表为空、重名、占用了保留名、或配置里的 ``name`` 与工具自身不一致。
        ReflectionError: 模块/属性解析不了，或解析出来的不是工具。
        TypeError: 工厂不接受配置里的某个键（键名写错的典型症状）。
    """
    if not app_config.tools:
        raise ValueError(_EMPTY_HINT)

    tools = [_resolve_entry(entry) for entry in app_config.tools]
    _check_names(tools)
    return tools


def _resolve_entry(entry: ToolConfig) -> BaseTool:
    """解析一项：``use`` 指向工具实例就直接用，指向工厂就用额外键调出来。"""
    resolved: Any = resolve_variable(entry.use)

    if isinstance(resolved, BaseTool):
        tool = resolved
    elif isinstance(resolved, Callable):
        tool = resolved(**(entry.model_extra or {}))
        if not isinstance(tool, BaseTool):
            raise ReflectionError(
                f"'{entry.use}' 不是工具：它作为工厂没有返回 BaseTool（返回了 {type(tool).__name__}）"
            )
    else:
        raise ReflectionError(f"'{entry.use}' 既不是 BaseTool 实例也不是工厂函数")

    if entry.name != tool.name:
        raise ValueError(
            f"配置里的 name={entry.name!r} 与工具自己的 .name={tool.name!r} 不一致（use: {entry.use}）。"
            "模型看到的是后者、路由查的是后者，改了配置却忘了改工具的话，模型会调用一个不存在的名字。"
        )
    return tool


def _check_names(tools: list[BaseTool]) -> None:
    """重名与保留名在这里拦掉。

    ``create_agent`` 对重名是**静默接受**的（ADR-0011 记为已知盲区），症状是模型收到两份
    同名 schema、调用落到哪一个不确定。注册表是唯一能在启动期看见它的位置。
    """
    seen: dict[str, str] = {}
    for tool in tools:
        if tool.name in seen:
            raise ValueError(
                f"工具重名：{tool.name!r} 被注册了两次（{seen[tool.name]} 与 {type(tool).__name__}）。"
                "重名会让模型收到歧义的 schema，改掉配置里的其中一个。"
            )
        if tool.name == TASK_TOOL_NAME:
            raise ValueError(
                f"业务工具不能叫 {TASK_TOOL_NAME!r}：主 Agent 用这个名字派发子 agent，撞名会把派发路由到它身上。"
            )
        seen[tool.name] = type(tool).__name__


__all__ = ["build_tools"]
