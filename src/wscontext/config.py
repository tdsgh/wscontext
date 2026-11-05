"""Loads `.wscontext/config.toml` into frozen dataclasses."""

import logging
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RedactionConfig:
    extra_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetentionConfig:
    raw_days: int = 180


@dataclass(frozen=True)
class SyncConfig:
    remote: str | None = None
    fetch_throttle_seconds: int = 300


@dataclass(frozen=True)
class HookConfig:
    time_budget_ms: int = 2000


@dataclass(frozen=True)
class CaptureConfig:
    precompact_snapshot: bool = False
    # a session idle for longer is not linked to a commit (open question 2)
    stale_session_minutes: int = 120
    # cut each stored tool result at this many characters; 0 keeps it whole
    tool_result_max_chars: int = 0


@dataclass(frozen=True)
class LogsConfig:
    max_bytes: int = 1_048_576


@dataclass(frozen=True)
class RetrievalConfig:
    # off turns the brief and `ctx find` off together
    enabled: bool = True
    brief: bool = True


@dataclass(frozen=True)
class StoreConfig:
    # size warnings; the store limit counts the local store and shared history separately
    session_warn_bytes: int = 10_485_760
    store_warn_bytes: int = 1_073_741_824


@dataclass(frozen=True)
class Config:
    redaction: RedactionConfig = field(default_factory=RedactionConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    hooks: HookConfig = field(default_factory=HookConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    logs: LogsConfig = field(default_factory=LogsConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    store: StoreConfig = field(default_factory=StoreConfig)


def _build(cls, data: dict, where: str):
    known = {f.name for f in fields(cls)}
    kwargs = {}
    for key, value in data.items():
        if key not in known:
            log.warning("unknown config key ignored: %s.%s", where, key)
        elif isinstance(value, list):
            kwargs[key] = tuple(value)
        else:
            kwargs[key] = value
    return cls(**kwargs)


def load_config(repo_root: Path) -> Config:
    path = repo_root / ".wscontext" / "config.toml"
    if not path.exists():
        return Config()
    with path.open("rb") as f:
        data = tomllib.load(f)
    sections = {"redaction": RedactionConfig, "retention": RetentionConfig,
                "sync": SyncConfig, "hooks": HookConfig,
                "capture": CaptureConfig, "logs": LogsConfig,
                "retrieval": RetrievalConfig, "store": StoreConfig}
    kwargs = {}
    for key, value in data.items():
        if key in sections and isinstance(value, dict):
            kwargs[key] = _build(sections[key], value, key)
        else:
            log.warning("unknown config key ignored: %s", key)
    return Config(**kwargs)
