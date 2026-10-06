"""Class-path reflection for pluggable extension points.

Every pluggable point (model, tool, sandbox, memory provider, ...) is selected
by a ``"module.path:attr"`` string in config and resolved here. No DI container.

Two resolvers, one import path: :func:`resolve_variable` for anything a module
exposes (a tool is a ``BaseTool`` **instance**, not a class), and
:func:`resolve_class` for the subset that must also be a class.
"""

from __future__ import annotations

import importlib
from typing import Any


class ReflectionError(Exception):
    """Raised when a class-path string cannot be resolved to a valid object."""


def resolve_variable[T](path: str, expected_type: type[T] | tuple[type, ...] | None = None) -> T:
    """Resolve ``"module.path:attr"`` to whatever that module attribute is.

    与 :func:`resolve_class` 并列而不是合并的**唯一**理由：扩展点里有一类是**实例**
    ——``@tool`` 装饰过的函数就是 ``BaseTool`` 实例——而 ``resolve_class`` 的
    ``isinstance(value, type)`` 会把它们全部拒掉。

    Args:
        path: A ``module.path:attr`` string.
        expected_type: When provided, the resolved value must be an instance of it.

    Raises:
        ReflectionError: malformed path, unimportable module, missing attribute,
            or (when ``expected_type`` is given) a value of the wrong type.
    """
    if ":" not in path:
        raise ReflectionError(f"Invalid variable path {path!r}: expected 'module.path:attr'")
    module_name, _, attr_name = path.partition(":")
    if not module_name or not attr_name:
        raise ReflectionError(f"Invalid variable path {path!r}: expected 'module.path:attr'")

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        # 「模块没装」是这条路上最常见的一种失败（可选依赖），所以原样带上原因。
        raise ReflectionError(f"Could not import module '{module_name}': {exc}") from exc

    try:
        value = getattr(module, attr_name)
    except AttributeError as exc:
        raise ReflectionError(f"Module '{module_name}' has no attribute '{attr_name}'") from exc

    if expected_type is not None and not isinstance(value, expected_type):
        raise ReflectionError(f"'{path}' resolves to {type(value).__name__}, not {_type_name(expected_type)}")
    return value


def resolve_class[T](path: str, base_class: type[T] | None = None) -> type[T]:
    """Resolve ``"module.path:ClassName"`` to a class.

    Args:
        path: A ``module.path:ClassName`` string.
        base_class: When provided, the resolved class must be a subclass of it.

    Raises:
        ReflectionError: malformed path, unimportable module, missing attribute,
            a non-class attribute, or (when ``base_class`` is given) a class
            that is not a subclass.
    """
    # 不带 expected_type：类检查在下面单独做，才留得住「不是类」这句更准的话。
    value: Any = resolve_variable(path)
    if not isinstance(value, type):
        raise ReflectionError(f"'{path}' does not resolve to a class")

    if base_class is not None and not issubclass(value, base_class):
        raise ReflectionError(
            f"'{path}' resolves to {value.__module__}.{value.__name__}, "
            f"which is not a subclass of {base_class.__module__}.{base_class.__name__}"
        )
    return value


def _type_name(expected: type | tuple[type, ...]) -> str:
    """把 expected_type 渲染成人读得懂的名字（元组就写成 ``A or B``）。"""
    if isinstance(expected, tuple):
        return " or ".join(t.__name__ for t in expected)
    return expected.__name__
