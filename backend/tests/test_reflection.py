"""Contract tests for evaharness.reflection.resolvers.

Pin the class-path reflection contract: `resolve_class("module.path:ClassName")`
with optional base-class validation, plus `resolve_variable` for the extension
points that are **instances** rather than classes (a tool is a `BaseTool`).
This is the single mechanism behind every pluggable extension point
(model/tool/sandbox/memory providers).
"""

import os
import pathlib

import pytest
from pydantic import BaseModel

from evaharness.reflection.resolvers import ReflectionError, resolve_class, resolve_variable


def test_resolve_class_from_stdlib():
    assert resolve_class("pathlib:Path") is pathlib.Path


def test_resolve_class_from_pydantic():
    assert resolve_class("pydantic:BaseModel") is BaseModel


def test_resolve_class_validates_base_class():
    assert resolve_class("pydantic:BaseModel", BaseModel) is BaseModel


def test_resolve_class_wrong_base_class_raises():
    with pytest.raises(ReflectionError):
        resolve_class("pathlib:Path", BaseModel)


def test_resolve_class_malformed_path_raises():
    with pytest.raises(ReflectionError):
        resolve_class("no-colon-here")


def test_resolve_class_missing_module_raises():
    with pytest.raises(ReflectionError):
        resolve_class("evaharness.nonexistent_module:Nothing")


def test_resolve_class_missing_attribute_raises():
    with pytest.raises(ReflectionError):
        resolve_class("pathlib:DoesNotExist")


def test_resolve_class_still_rejects_a_non_class():
    """委托给 resolve_variable 之后，「不是类」这句话还得在。"""
    with pytest.raises(ReflectionError, match="does not resolve to a class"):
        resolve_class("os:sep")


# --------------------------------------------------------------------------- #
# resolve_variable（工具是实例，不是类，所以另开一个）
# --------------------------------------------------------------------------- #


def test_resolve_variable_returns_whatever_the_module_exposes():
    assert resolve_variable("os:sep") == os.sep
    assert resolve_variable("pathlib:Path") is pathlib.Path


def test_resolve_variable_returns_instances():
    """**这正是 resolve_class 表达不了的那一类。**"""
    from app.tools.builtin import get_current_time

    assert resolve_variable("app.tools.builtin:get_current_time") is get_current_time


def test_resolve_variable_validates_the_expected_type():
    from app.tools.builtin import get_current_time

    assert resolve_variable("app.tools.builtin:get_current_time", object) is get_current_time
    with pytest.raises(ReflectionError, match="not dict"):
        resolve_variable("app.tools.builtin:get_current_time", dict)


def test_resolve_variable_malformed_path_raises():
    with pytest.raises(ReflectionError):
        resolve_variable("no-colon-here")


def test_resolve_variable_missing_module_raises():
    with pytest.raises(ReflectionError, match="Could not import module"):
        resolve_variable("evaharness.nonexistent_module:Nothing")


def test_resolve_variable_missing_attribute_raises():
    with pytest.raises(ReflectionError):
        resolve_variable("pathlib:DoesNotExist")
