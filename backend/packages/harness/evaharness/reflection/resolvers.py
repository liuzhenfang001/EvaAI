"""Class-path reflection for pluggable extension points.

Every pluggable point (model, tool, sandbox, memory provider, ...) is selected
by a ``"module.path:ClassName"`` string in config and resolved here. No DI
container.
"""

from __future__ import annotations

import importlib


class ReflectionError(Exception):
    """Raised when a class-path string cannot be resolved to a valid class."""


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
    if ":" not in path:
        raise ReflectionError(f"Invalid class path {path!r}: expected 'module.path:ClassName'")
    module_name, _, attr_name = path.partition(":")
    if not module_name or not attr_name:
        raise ReflectionError(f"Invalid class path {path!r}: expected 'module.path:ClassName'")

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ReflectionError(f"Could not import module '{module_name}': {exc}") from exc

    try:
        cls = getattr(module, attr_name)
    except AttributeError as exc:
        raise ReflectionError(f"Module '{module_name}' has no attribute '{attr_name}'") from exc

    if not isinstance(cls, type):
        raise ReflectionError(f"'{path}' does not resolve to a class")

    if base_class is not None and not issubclass(cls, base_class):
        raise ReflectionError(
            f"'{path}' resolves to {cls.__module__}.{cls.__name__}, "
            f"which is not a subclass of {base_class.__module__}.{base_class.__name__}"
        )
    return cls
