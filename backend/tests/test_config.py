"""Contract tests for evaharness.config.loader.

These tests pin the config-loading contract before the implementation exists:
three layers (.env -> config.yaml -> extensions_config.json), `$ENV` reference
resolution, and config_version validation. No hot reload.
"""

import json
import os

import pytest

from evaharness.config.loader import (
    CURRENT_CONFIG_VERSION,
    ConfigError,
    ConfigVersionError,
    load_config,
    load_env_file,
    load_extensions,
    resolve_env_references,
)


def test_resolve_env_references_replaces_dollar_strings(monkeypatch):
    monkeypatch.setenv("EVA_API_KEY", "secret-123")
    data = {
        "key": "$EVA_API_KEY",
        "nested": {"k2": "$EVA_API_KEY"},
        "plain": "not-a-var",
        "num": 42,
    }
    assert resolve_env_references(data) == {
        "key": "secret-123",
        "nested": {"k2": "secret-123"},
        "plain": "not-a-var",
        "num": 42,
    }


def test_resolve_env_references_missing_var_raises(monkeypatch):
    monkeypatch.delenv("EVA_NOPE", raising=False)
    with pytest.raises(ConfigError):
        resolve_env_references({"k": "$EVA_NOPE"})


def test_load_config_resolves_env_and_validates_version(tmp_path, monkeypatch):
    monkeypatch.setenv("EVA_MODEL_KEY", "sk-123")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"config_version: {CURRENT_CONFIG_VERSION}\n"
        "log_level: info\n"
        "models:\n"
        "  - api_key: $EVA_MODEL_KEY\n",
        encoding="utf-8",
    )
    result = load_config(cfg)
    assert result["config_version"] == CURRENT_CONFIG_VERSION
    assert result["log_level"] == "info"
    assert result["models"][0]["api_key"] == "sk-123"


def test_load_config_version_mismatch_raises(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"config_version: {CURRENT_CONFIG_VERSION + 999}\n", encoding="utf-8")
    with pytest.raises(ConfigVersionError):
        load_config(cfg)


def test_load_config_missing_version_raises(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("log_level: info\n", encoding="utf-8")
    with pytest.raises(ConfigVersionError):
        load_config(cfg)


def test_load_config_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "does-not-exist.yaml")


def test_load_extensions(tmp_path):
    ext = tmp_path / "extensions_config.json"
    ext.write_text(json.dumps({"mcpServers": {}, "skills": {}}), encoding="utf-8")
    assert load_extensions(ext) == {"mcpServers": {}, "skills": {}}


def test_load_env_file_populates_environment(tmp_path):
    env = tmp_path / ".env"
    env.write_text("EVA_SECRET=abc\nOTHER=def\n", encoding="utf-8")
    load_env_file(env)
    assert os.environ["EVA_SECRET"] == "abc"
    assert os.environ["OTHER"] == "def"
