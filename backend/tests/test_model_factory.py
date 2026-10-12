"""Contract tests for evaharness.models.factory."""

import pytest
from langchain_openai import ChatOpenAI

from evaharness.config.app_config import AppConfig, ModelConfig
from evaharness.models.factory import create_chat_model, thinking_model_kwargs
from evaharness.runtime import RunOptions


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


# --------------------------------------------------------------------------- #
# 思考档位 → 模型构造参数
# --------------------------------------------------------------------------- #


def test_thinking_off_turns_thinking_off():
    kwargs = thinking_model_kwargs("off")
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert kwargs["reasoning_effort"] == "minimal"


@pytest.mark.parametrize("level", ["low", "high", "max"])
def test_each_thinking_level_turns_thinking_on_with_that_effort(level):
    """三档的取值与 DeepSeek 认的力度**完全一致**——传一个它不认的值只会换来 400。"""
    kwargs = thinking_model_kwargs(level)
    assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}
    assert kwargs["reasoning_effort"] == level


def test_options_reach_the_model_constructor():
    cfg = _cfg(dict(name="a", model="m-a", api_key="sk-test"))
    model = create_chat_model(cfg, "a", options=RunOptions(thinking="max"))
    assert model.extra_body == {"thinking": {"type": "enabled"}}
    assert model.reasoning_effort == "max"


def test_a_profile_extra_body_survives_the_run_options():
    """**这条钉住一个真写出来过的 bug**。

    ``settings.update(kwargs)`` 是浅的：运行参数里的 ``extra_body`` 会把 profile 里配好的
    整个冲掉，症状是「一开思考，之前配的厂商参数就没了」——而且不报错。
    """
    cfg = _cfg(dict(name="a", model="m-a", api_key="sk-test", extra_body={"top_k": 5}))
    model = create_chat_model(cfg, "a", options=RunOptions(thinking="high"))

    assert model.extra_body["top_k"] == 5, "profile 里的其它 extra_body 键被冲掉了"
    assert model.extra_body["thinking"] == {"type": "enabled"}


def test_without_options_nothing_is_added():
    """不带运行参数时行为与老调用方**完全一致**——封存的 Plan-Exec 路由与所有测试都靠这条。"""
    cfg = _cfg(dict(name="a", model="m-a", api_key="sk-test"))
    model = create_chat_model(cfg, "a")
    assert model.extra_body is None
    assert model.reasoning_effort is None
