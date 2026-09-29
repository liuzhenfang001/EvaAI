"""Contract tests for evaharness.config.app_config."""

import pytest
from pydantic import ValidationError

from evaharness.config.app_config import AppConfig, CheckpointConfig, JournalConfig, ModelConfig


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


def test_journal_config_defaults_to_jsonl():
    """默认落盘而非内存：不落盘的话「回放」无从谈起。"""
    journal = JournalConfig()
    assert journal.store == "evaharness.journal.stores:JsonlJournalStore"
    assert journal.flush_threshold == 20
    assert journal.dir is None, "缺省交给 store 自己的默认值，换实现时不会炸"


def test_app_config_journal_defaults_when_section_absent():
    """老 config.yaml 不带 journal 段也要能跑起来。"""
    cfg = AppConfig(config_version=1, models=[ModelConfig(name="a", model="m-a")])
    assert cfg.journal.store.endswith("JsonlJournalStore")


def test_app_config_journal_section_is_parsed():
    cfg = AppConfig(
        config_version=1,
        models=[ModelConfig(name="a", model="m-a")],
        journal={"store": "evaharness.journal.stores:MemoryJournalStore", "flush_threshold": 5},
    )
    assert cfg.journal.store.endswith("MemoryJournalStore")
    assert cfg.journal.flush_threshold == 5
