"""日志配置单测：级别解析、命名空间隔离、格式、幂等、回落警告。

**每个用例用自己独有的命名空间**（``evaharness_test_*``）——配置函数改的是**全局** logger，
共用名字会互相串。``stream`` 参数让断言不必去翻真的 stderr。
"""

from __future__ import annotations

import io
import logging

import pytest

from evaharness.logging import configure_logging, is_known_level, resolve_level


def _level_of(name: str) -> int:
    return logging.getLogger(name).level


# --------------------------------------------------------------------------- #
# 级别解析
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("debug", logging.DEBUG),
        ("INFO", logging.INFO),
        ("Warning", logging.WARNING),
        ("error", logging.ERROR),
        ("critical", logging.CRITICAL),
        ("20", 20),
        (logging.DEBUG, logging.DEBUG),
    ],
)
def test_resolve_level(given, expected):
    assert resolve_level(given) == expected


@pytest.mark.parametrize("given", ["chatty", "", None, "loud"])
def test_unknown_level_falls_back_to_info(given):
    """配置写错**不抛**——服务不该因为一个日志级别拼错就起不来。"""
    assert resolve_level(given) == logging.INFO
    assert not is_known_level(given)


@pytest.mark.parametrize("given", ["info", "DEBUG", "30", 10])
def test_known_level(given):
    assert is_known_level(given)


# --------------------------------------------------------------------------- #
# 配置生效
# --------------------------------------------------------------------------- #


def test_sets_level_for_children_and_stops_propagation():
    configure_logging("warning", namespaces=("evaharness_test_level",), stream=io.StringIO())

    assert _level_of("evaharness_test_level") == logging.WARNING
    child = logging.getLogger("evaharness_test_level.child")
    assert child.getEffectiveLevel() == logging.WARNING  # 子 logger 自己没设级别，继承顶层
    assert child.isEnabledFor(logging.WARNING)
    assert not child.isEnabledFor(logging.INFO), "warning 档不该放 INFO 出来"
    assert logging.getLogger("evaharness_test_level").propagate is False


def test_records_are_formatted():
    stream = io.StringIO()
    configure_logging("debug", namespaces=("evaharness_test_emit",), stream=stream)

    logging.getLogger("evaharness_test_emit.mod").info("你好 %s", "世界")

    out = stream.getvalue()
    assert "INFO" in out and "evaharness_test_emit.mod" in out and "你好 世界" in out
    # 有时间戳（默认格式的第一段是 asctime）。
    assert out.split()[0][:4].isdigit()


def test_info_is_filtered_below_the_level():
    stream = io.StringIO()
    configure_logging("warning", namespaces=("evaharness_test_quiet",), stream=stream)

    logging.getLogger("evaharness_test_quiet.mod").info("不该出现")
    logging.getLogger("evaharness_test_quiet.mod").warning("该出现")

    out = stream.getvalue()
    assert "不该出现" not in out and "该出现" in out


def test_returns_the_resolved_level():
    assert configure_logging("DEBUG", namespaces=("evaharness_test_ret",)) == logging.DEBUG


def test_repeated_calls_do_not_stack_handlers():
    """lifespan 在测试里会被跑很多遍——重复调用只替换我们自己那个 handler。"""
    name = "evaharness_test_idem"
    configure_logging("info", namespaces=(name,), stream=io.StringIO())
    configure_logging("debug", namespaces=(name,), stream=io.StringIO())

    assert len(logging.getLogger(name).handlers) == 1
    assert _level_of(name) == logging.DEBUG


def test_does_not_touch_other_namespaces():
    """只配给它的命名空间——别人的 logger 一个都不碰（否则会放开 httpx 那种刷屏日志）。"""
    other = logging.getLogger("evaharness_test_other")
    handlers_before, level_before = list(other.handlers), other.level

    configure_logging("debug", namespaces=("evaharness_test_isolated",), stream=io.StringIO())

    assert other.handlers == handlers_before
    assert other.level == level_before


def test_unknown_level_emits_a_warning():
    stream = io.StringIO()
    configure_logging("nonsense", namespaces=("evaharness_test_warn",), stream=stream)

    out = stream.getvalue()
    assert "nonsense" in out
    assert "WARNING" in out
