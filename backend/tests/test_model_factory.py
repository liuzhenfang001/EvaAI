"""Contract tests for evaharness.models.factory."""

import pytest
from langchain_openai import ChatOpenAI

from evaharness.config.app_config import AppConfig, ModelConfig
from evaharness.models.factory import create_chat_model


def _cfg(*model_kwargs):
    return AppConfig(config_version=1, models=[ModelConfig(**kw) for kw in model_kwargs])


def test_create_chat_model_deepseek():
    cfg = _cfg(
        dict(
            name="deepseek-chat",
            model="deepseek-chat",
            base_url="https://api.deepseek.com/v1",
            api_key="sk-123",
            temperature=0.3,
        )
    )
    model = create_chat_model(cfg, "deepseek-chat")
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "deepseek-chat"
    assert model.openai_api_base == "https://api.deepseek.com/v1"
    assert model.openai_api_key.get_secret_value() == "sk-123"
    assert model.temperature == 0.3


def test_create_chat_model_defaults_to_first():
    cfg = _cfg(dict(name="a", model="m-a", api_key="sk-test"), dict(name="b", model="m-b", api_key="sk-test"))
    assert create_chat_model(cfg).model_name == "m-a"


def test_create_chat_model_missing_raises():
    cfg = _cfg(dict(name="a", model="m-a"))
    with pytest.raises(ValueError):
        create_chat_model(cfg, "nope")


def test_create_chat_model_kwargs_override():
    cfg = _cfg(dict(name="a", model="m-a", temperature=0.0, api_key="sk-test"))
    model = create_chat_model(cfg, "a", temperature=0.9)
    assert model.temperature == 0.9


def test_create_chat_model_does_not_leak_profile_name():
    cfg = _cfg(dict(name="a", model="m-a", api_key="sk-test"))
    model = create_chat_model(cfg, "a")
    assert model.name != "a"
