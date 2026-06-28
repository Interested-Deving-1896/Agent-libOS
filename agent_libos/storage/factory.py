from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, unquote, urlsplit, urlunsplit

from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig
from agent_libos.models.exceptions import ValidationError
from agent_libos.storage.base import RuntimeStore
from agent_libos.storage.sqlite import SQLiteStore

POSTGRES_SCHEMES = {"postgres", "postgresql"}
SQLITE_SCHEME = "sqlite"


def open_store(target: str | Path | None = None, *, config: AgentLibOSConfig | None = None) -> RuntimeStore:
    selected_config = config or DEFAULT_CONFIG
    selected_target = _selected_target(target, selected_config)
    backend = _backend_for(selected_target, selected_config, explicit=target is not None)
    if backend == "postgres":
        from agent_libos.storage.postgres import PostgresStore

        dsn = _postgres_dsn(selected_target, selected_config)
        return PostgresStore(dsn, config=selected_config)
    if backend == "sqlite":
        return SQLiteStore(_sqlite_target(selected_target), config=selected_config)
    raise ValidationError(f"unsupported runtime store backend: {backend}")


def display_store_target(target: str | Path | None = None, *, config: AgentLibOSConfig | None = None) -> str:
    selected_config = config or DEFAULT_CONFIG
    selected_target = _selected_target(target, selected_config)
    backend = _backend_for(selected_target, selected_config, explicit=target is not None)
    if backend == "postgres":
        return redact_store_target(_postgres_dsn(selected_target, selected_config))
    return str(selected_target)


def redact_store_target(target: str | Path) -> str:
    text = str(target)
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in POSTGRES_SCHEMES or not parsed.netloc:
        return text
    userinfo, sep, hostinfo = parsed.netloc.rpartition("@")
    if not sep:
        return text
    user, colon, password = userinfo.partition(":")
    if not colon or not password:
        return text
    return urlunsplit(SplitResult(parsed.scheme, f"{user}:***@{hostinfo}", parsed.path, parsed.query, parsed.fragment))


def _selected_target(target: str | Path | None, config: AgentLibOSConfig) -> str | Path:
    if target is not None:
        return target
    if config.runtime.store_backend == "postgres":
        if not config.runtime.store_dsn:
            raise ValidationError("PostgreSQL runtime store requires runtime.store_dsn")
        return config.runtime.store_dsn
    return config.runtime.local_store_target


def _backend_for(target: str | Path, config: AgentLibOSConfig, *, explicit: bool = False) -> str:
    text = str(target)
    parsed = urlsplit(text)
    scheme = parsed.scheme.lower()
    if scheme in POSTGRES_SCHEMES:
        return "postgres"
    if scheme == SQLITE_SCHEME:
        return "sqlite"
    if explicit:
        return "sqlite"
    return config.runtime.store_backend


def _postgres_dsn(target: str | Path, config: AgentLibOSConfig) -> str:
    text = str(target)
    parsed = urlsplit(text)
    if parsed.scheme.lower() in POSTGRES_SCHEMES:
        return text
    if config.runtime.store_dsn:
        return config.runtime.store_dsn
    raise ValidationError("PostgreSQL runtime store requires a postgresql:// DSN")


def _sqlite_target(target: str | Path) -> str:
    text = str(target)
    parsed = urlsplit(text)
    if parsed.scheme.lower() == SQLITE_SCHEME:
        if parsed.netloc and parsed.path:
            return unquote(f"//{parsed.netloc}{parsed.path}")
        if parsed.path:
            path = unquote(parsed.path)
            if path.startswith("/") and len(path) > 2 and path[2] == ":":
                return path[1:]
            return path
        return ":memory:"
    return ":memory:" if text in {"local", ":memory:"} else text
