"""Loads `.wscontext/config.toml` into frozen dataclasses."""

import json
import logging
import re
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
    # the first prompt of a session gets a brief of related past sessions; enabled = false overrides
    prompt_brief: bool = True


@dataclass(frozen=True)
class StoreConfig:
    # size warnings; the store limit counts the local store and shared history separately
    session_warn_bytes: int = 10_485_760
    store_warn_bytes: int = 1_073_741_824


@dataclass(frozen=True)
class GraphConfig:
    # an unknown language name is kept so the health check can report it
    languages: tuple[str, ...] = ("python", "typescript", "javascript", "rust")
    # glob patterns on repository paths
    exclude: tuple[str, ...] = ()
    # the longest a command waits for the graph
    query_budget_ms: int = 5000
    # how many recent non-merge commits count toward co-change
    cochange_commits: int = 2000


@dataclass(frozen=True)
class DocumentsConfig:
    # repository paths of the Markdown files `ctx find` searches; committed, so every clone searches the same
    paths: tuple[str, ...] = ()


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
    graph: GraphConfig = field(default_factory=GraphConfig)
    documents: DocumentsConfig = field(default_factory=DocumentsConfig)


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
                "retrieval": RetrievalConfig, "store": StoreConfig,
                "graph": GraphConfig, "documents": DocumentsConfig}
    kwargs = {}
    for key, value in data.items():
        if key in sections and isinstance(value, dict):
            kwargs[key] = _build(sections[key], value, key)
        else:
            log.warning("unknown config key ignored: %s", key)
    return Config(**kwargs)


def _array_end(text: str, start: int) -> int:
    """Offset just past the array whose `[` is at `start`, skipping strings and comments."""
    depth, i = 0, start
    while i < len(text):
        c = text[i]
        if c in "\"'":
            i += 1
            while i < len(text) and text[i] != c and text[i] != "\n":
                i += 2 if c == '"' and text[i] == "\\" else 1
        elif c == "#":
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unterminated array in [documents] paths")


def set_document_paths(root: Path, paths: list[str]) -> None:
    """Set `[documents] paths` in the committed config as a text edit; every other byte stays."""
    from wscontext.store.records import atomic_write  # records imports this module

    path = root / ".wscontext" / "config.toml"
    text = path.read_bytes().decode("utf-8") if path.exists() else ""
    eol = "\r\n" if "\r\n" in text else "\n"
    if paths:
        value = "[" + eol + "".join(f"  {json.dumps(p, ensure_ascii=False)},{eol}" for p in paths) + "]"
    else:
        value = "[]"
    header = re.search(r"^[ \t]*\[documents\][ \t]*(#[^\n]*)?$", text, re.M)
    if header is None:
        sep = "" if not text else (eol if text.endswith("\n") else eol * 2)
        new = f"{text}{sep}[documents]{eol}paths = {value}{eol}"
    else:
        body = header.end()
        nxt = re.search(r"^[ \t]*\[", text[body:], re.M)
        end = body + nxt.start() if nxt else len(text)
        key = re.search(r"^[ \t]*paths[ \t]*=[ \t]*", text[body:end], re.M)
        if key is None:
            new = f"{text[:body]}{eol}paths = {value}{text[body:]}"
        else:
            vstart = body + key.end()
            if text[vstart:vstart + 1] != "[":
                raise ValueError("[documents] paths is not a list")
            new = text[:vstart] + value + text[_array_end(text, vstart):]
    try:
        back = tomllib.loads(new).get("documents", {}).get("paths")
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"edited config does not parse: {e}") from None
    if back != list(paths):
        raise ValueError("edited config does not load back to the given paths")
    atomic_write(path, new.encode("utf-8"))
