from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter

from agent_libos.config.defaults import DEFAULT_CONFIG, AgentLibOSConfig

_CONFIG_ADAPTER = TypeAdapter(AgentLibOSConfig)


def load_config_file(path: str | Path, *, base: AgentLibOSConfig = DEFAULT_CONFIG) -> AgentLibOSConfig:
    """Load a YAML config file as a strict overlay on top of ``base``."""

    selected = Path(path)
    if not selected.exists():
        raise FileNotFoundError(f"config file not found: {selected}")
    try:
        data = yaml.safe_load(selected.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML config {selected}: {exc}") from exc
    if data is None:
        overlay: Mapping[str, Any] = {}
    elif isinstance(data, Mapping):
        overlay = data
    else:
        raise ValueError(f"config YAML root must be a mapping: {selected}")

    merged = _deep_merge(asdict(base), overlay)
    return _CONFIG_ADAPTER.validate_python(merged)


def load_config_from_cwd(
    filename: str | Path = "config.yaml", *, base: AgentLibOSConfig = DEFAULT_CONFIG
) -> AgentLibOSConfig:
    """Load ``filename`` from the current working directory when it exists."""

    selected = Path.cwd() / Path(filename)
    if not selected.exists():
        return base
    return load_config_file(selected, base=base)


def _deep_merge(base: Any, overlay: Any) -> Any:
    if isinstance(base, Mapping) and isinstance(overlay, Mapping):
        merged = dict(base)
        for key, value in overlay.items():
            if key in merged:
                merged[key] = _deep_merge(merged[key], value)
            else:
                merged[key] = value
        return merged
    return overlay
