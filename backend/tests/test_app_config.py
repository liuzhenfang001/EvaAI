"""Contract tests for evaharness.config.app_config."""

import pytest
from pydantic import ValidationError

from evaharness.config.app_config import AppConfig, CheckpointConfig, JournalConfig, ModelConfig, ToolConfig


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


# --------------------------------------------------------------------------- #
# 工具注册表（T4）
# --------------------------------------------------------------------------- #


def test_tool_config_keeps_extra_keys_for_the_factory():
    """额外键是**要给工厂的**，所以不能 forbid——写错的键由工厂的签名去拒。"""
    tool = ToolConfig(name="web_search", use="app.tools.web:make_web_search", max_results=3, timeout=20)
    assert tool.use == "app.tools.web:make_web_search"
    assert tool.model_extra == {"max_results": 3, "timeout": 20}


def test_tool_config_requires_a_use_path():
    with pytest.raises(ValidationError):
        ToolConfig(name="x")  # type: ignore[call-arg]


def test_app_config_tools_default_to_empty():
    """缺省是空表，而空表会在启动期被拒绝——这里只钉住类型，不钉住「可用」。

    刻意不像 journal 那样给内置默认集：那会让「哪些工具在跑」有两个来源，其中一个隐形。
    """
    cfg = AppConfig(config_version=1, models=[ModelConfig(name="a", model="m-a")])
    assert cfg.tools == []


def test_app_config_tools_section_is_parsed():
    cfg = AppConfig(
        config_version=1,
        models=[ModelConfig(name="a", model="m-a")],
        tools=[{"name": "get_current_time", "use": "app.tools.builtin:get_current_time"}],
    )
    assert cfg.tools[0].name == "get_current_time"


def test_knowledge_config_defaults_are_off():
    """知识库默认**关**：没有嵌入 key 时，一个可选功能不该让服务起不来。"""
    cfg = AppConfig(config_version=1, models=[ModelConfig(name="a", model="m-a")])
    assert cfg.knowledge.enabled is False
    assert cfg.knowledge.source == "./knowledge"
    assert cfg.knowledge.persist_dir == "./data/knowledge"
    assert cfg.knowledge.embedding.model == "text-embedding-v4"
    assert cfg.knowledge.embedding.dimensions == 1024
    assert cfg.knowledge.embedding.batch_size == 10
    assert cfg.knowledge.top_k == 4


def test_knowledge_section_is_parsed():
    cfg = AppConfig(
        config_version=1,
        models=[ModelConfig(name="a", model="m-a")],
        knowledge={
            "enabled": True,
            "source": "./docs-kb",
            "chunk_size": 500,
            "ignore": ["drafts/*"],
            "embedding": {"api_key": "k", "dimensions": 512},
        },
    )
    assert cfg.knowledge.enabled is True
    assert cfg.knowledge.source == "./docs-kb"
    assert cfg.knowledge.chunk_size == 500
    assert cfg.knowledge.ignore == ["drafts/*"]
    assert cfg.knowledge.embedding.api_key == "k"
    assert cfg.knowledge.embedding.dimensions == 512
    # 没覆盖的键保留默认。
    assert cfg.knowledge.chunk_overlap == 120
    assert cfg.knowledge.embedding.model == "text-embedding-v4"
