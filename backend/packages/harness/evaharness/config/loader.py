"""Configuration loading: three layers, startup-only, versioned.

Layers (loaded in order):
1. ``.env``                  -> ``os.environ`` (secrets)
2. ``config.yaml``           -> main config; ``$ENV`` references resolved,
   ``config_version`` validated
3. ``extensions_config.json`` -> extensions (MCP / skills / middlewares)

No hot reload: config is loaded once at startup.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

CURRENT_CONFIG_VERSION = 1


class ConfigError(Exception):
    """Base error raised when configuration cannot be loaded."""


class ConfigVersionError(ConfigError):
    """config.yaml declares a config_version this build does not support."""


def resolve_env_references(value: Any) -> Any:
    """Recursively replace ``$VAR`` strings with their environment value.

    Only strings whose first character is ``$`` are treated as references;
    everything else is returned unchanged. Dict/list containers are recursed
    into. A referenced variable that is not set raises :class:`ConfigError`.
    """
    if isinstance(value, str):
        if value.startswith("$"):
            name = value[1:]
            if not name:
                return value
            if name not in os.environ:
                raise ConfigError(f"Environment variable '{name}' referenced but not set")
            return os.environ[name]
        return value
    if isinstance(value, dict):
        return {key: resolve_env_references(item) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_env_references(item) for item in value]
    return value


def load_config(config_path: Path | str) -> dict[str, Any]:
    """Load ``config.yaml``, resolve env references, validate ``config_version``."""
    path = Path(config_path)
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Failed to parse config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Config file {path} must contain a YAML mapping")
    if "config_version" not in data:
        raise ConfigVersionError(f"Config file {path} is missing 'config_version'")
    if data["config_version"] != CURRENT_CONFIG_VERSION:
        raise ConfigVersionError(
            f"Config file {path} has config_version {data['config_version']}, "
            f"but this build supports {CURRENT_CONFIG_VERSION}. "
            f"Run the config upgrade step."
        )
    return resolve_env_references(data)


def load_extensions(extensions_path: Path | str) -> dict[str, Any]:
    """Load ``extensions_config.json`` (no env resolution, no version check)."""
    path = Path(extensions_path)
    if not path.is_file():
        raise ConfigError(f"Extensions config file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Failed to parse extensions config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Extensions config {path} must contain a JSON object")
    return data


def load_env_file(env_path: Path | str) -> None:
    """Load a ``.env`` file into ``os.environ`` (does not override existing values)."""
    path = Path(env_path)
    if not path.is_file():
        raise ConfigError(f"Env file not found: {path}")
    load_dotenv(path, override=False)
