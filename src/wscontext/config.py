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
    # on until the PreCompact open question is answered
    precompact_snapshot: bool = True


@dataclass(frozen=True)
class LogsConfig:
    max_bytes: int = 1_048_576


@dataclass(frozen=True)
class Config:
    redaction: RedactionConfig = field(default_factory=RedactionConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    hooks: HookConfig = field(default_factory=HookConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    logs: LogsConfig = field(default_factory=LogsConfig)


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
                "capture": CaptureConfig, "logs": LogsConfig}
    kwargs = {}
    for key, value in data.items():
        if key in sections and isinstance(value, dict):
            kwargs[key] = _build(sections[key], value, key)
        else:
            log.warning("unknown config key ignored: %s", key)
    return Config(**kwargs)
