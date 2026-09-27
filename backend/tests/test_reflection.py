"""Contract tests for evaharness.reflection.resolvers.

Pin the class-path reflection contract: `resolve_class("module.path:ClassName")`
with optional base-class validation. This is the single mechanism behind every
pluggable extension point (model/tool/sandbox/memory providers).
"""

import pathlib

import pytest
from pydantic import BaseModel

from evaharness.reflection.resolvers import ReflectionError, resolve_class


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
