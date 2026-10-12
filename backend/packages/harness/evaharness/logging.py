"""把 ``config.yaml`` 的 ``log_level`` 真正接到我们的 logger 上。

**为什么需要它**：``log_level`` 曾经是一处**死配置**——声明了却没有任何消费方。而 uvicorn 的默认
日志配置只配 ``uvicorn`` / ``uvicorn.error`` / ``uvicorn.access`` 三个 logger，**从不碰 root**，
所以我们的 logger（``app.*`` / ``evaharness.*``）冒泡到 root 后：root 没有 handler、级别是 Python
默认的 WARNING，于是 **INFO / DEBUG 全被丢掉**；WARNING 以上靠 ``logging.lastResort`` 出来，
但那是裸 handler，**没有任何格式**（没时间戳、没级别、没 logger 名）。

**为什么只配自己的命名空间，不用 ``logging.basicConfig``**：``basicConfig`` 动的是 root，会把
第三方的日志一起放开——httpx 每个请求一条 INFO、chromadb 也有一堆——很快就刷屏，真正想看的那几条
反而淹了。这里只对我们自己的命名空间设级别、挂 handler、``propagate=False``，别人的 logger 不动。

**幂等**：重复调用（lifespan 在测试里会被跑很多遍）只替换我们自己挂上去的那个 handler，
不会越挂越多、把同一条日志打两遍。
"""

from __future__ import annotations

import logging
import sys
from typing import TextIO

DEFAULT_NAMESPACES: tuple[str, ...] = ("app", "evaharness")
"""要接管的 logger 命名空间的**顶层**——子 logger（``app.tools.knowledge`` 之类）继承它的级别与 handler。"""

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_HANDLER_MARK = "_evaai_log_handler"
"""打在我们自己挂的 handler 上的记号，用来在重复调用时认出「这是上次那个」。"""

_LEVELS = logging.getLevelNamesMapping()


def resolve_level(level: str | int) -> int:
    """把配置里的级别解析成 logging 的级别常量。

    接受级别名（大小写不敏感）、数字字符串、或直接的数字。**认不出来不抛**——配置写错不该让
    服务起不来，回落到 INFO（由 :func:`configure_logging` 记一条警告点名）。
    """
    if isinstance(level, int) and not isinstance(level, bool):
        return level
    name = str(level or "").strip().upper()
    if name.isdigit():
        return int(name)
    return _LEVELS.get(name, logging.INFO)


def is_known_level(level: str | int) -> bool:
    """这个写法认不认。``configure_logging`` 靠它决定要不要记那条回落警告。"""
    if isinstance(level, int) and not isinstance(level, bool):
        return True
    name = str(level or "").strip().upper()
    return name.isdigit() or name in _LEVELS


def configure_logging(
    level: str | int = "info",
    *,
    namespaces: tuple[str, ...] = DEFAULT_NAMESPACES,
    stream: TextIO | None = None,
) -> int:
    """按 ``level`` 配置 ``namespaces`` 下的 logger，返回解析出的级别。

    Args:
        level: 级别名或数字，来自 ``config.yaml`` 的 ``log_level``。
        namespaces: 要接管的顶层命名空间（默认 ``app`` 与 ``evaharness``）。
        stream: 日志去处，缺省 ``sys.stderr``（测试用它接一个 ``StringIO``）。

    Returns:
        解析出的级别常量（调用方可以据此断言配置生效）。
    """
    target = resolve_level(level)
    formatter = logging.Formatter(LOG_FORMAT)

    for name in namespaces:
        logger = logging.getLogger(name)
        for handler in [h for h in logger.handlers if getattr(h, _HANDLER_MARK, False)]:
            logger.removeHandler(handler)
        handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
        setattr(handler, _HANDLER_MARK, True)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(target)
        # 不回传给 root：否则哪天 root 有了 handler，同一条日志会被打两遍。
        logger.propagate = False

    if not is_known_level(level):
        logging.getLogger(namespaces[0] if namespaces else "").warning(
            "log_level 的写法不认识：%r，已回落到 INFO", level
        )
    return target


__all__ = ["DEFAULT_NAMESPACES", "LOG_FORMAT", "configure_logging", "is_known_level", "resolve_level"]
