"""Contract tests for evaharness.config.app_config."""

import pytest
from pydantic import ValidationError

from evaharness.config.app_config import AppConfig, CheckpointConfig, ModelConfig


def test_model_config_defaults():
    model = ModelConfig(name="m1", model="deepseek-chat")
    assert model.use == "langchain_openai:ChatOpenAI"
    assert model.temperature == 0.0
    assert model.base_url is None
    assert model.api_key is None


def test_model_config_extra_allow():
    model = ModelConfig(name="m1", model="deepseek-chat", max_tokens=4096)
    assert model.max_tokens == 4096


def test_app_config_requires_models():
    with pytest.raises(ValidationError):
        AppConfig(config_version=1)


def test_app_config_extra_allow():
    cfg = AppConfig(
        config_version=1,
        models=[ModelConfig(name="a", model="m-a")],
        future_section={"x": 1},
    )
    assert cfg.future_section == {"x": 1}


def test_get_model_config():
    cfg = AppConfig(
        config_version=1,
        models=[ModelConfig(name="a", model="m-a"), ModelConfig(name="b", model="m-b")],
    )
    assert cfg.get_model_config("a").model == "m-a"
    assert cfg.get_model_config("b").model == "m-b"
    assert cfg.get_model_config("zzz") is None


def test_checkpoint_config_defaults():
    checkpoint = CheckpointConfig()
    assert checkpoint.db_path == "./data/checkpoints.sqlite"
    assert checkpoint.retention_days == 7


def test_app_config_checkpoint_defaults_when_section_absent():
    """老 config.yaml 不带 checkpoint 段也要能跑起来。"""
    cfg = AppConfig(config_version=1, models=[ModelConfig(name="a", model="m-a")])
    assert cfg.checkpoint.retention_days == 7


def test_app_config_checkpoint_section_is_parsed():
    cfg = AppConfig(
        config_version=1,
        models=[ModelConfig(name="a", model="m-a")],
        checkpoint={"db_path": "/tmp/x.sqlite", "retention_days": 1},
    )
    assert cfg.checkpoint.db_path == "/tmp/x.sqlite"
    assert cfg.checkpoint.retention_days == 1
