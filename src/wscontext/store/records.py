"""Session, turn and checkpoint records: versioned JSON files, written atomically."""

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import ClassVar

from wscontext.redact.redactor import redact_value
from wscontext.config import Config
from wscontext.store.paths import StorePaths, is_safe_name


class RecordError(ValueError):
    pass


@dataclass(kw_only=True)
class TurnRecord:
    CURRENT_SCHEMA: ClassVar[int] = 1
    session_id: str
    author: str
    index: int
    started_at: str
    ended_at: str | None = None
    tool_calls: dict[str, int] = field(default_factory=dict)
    tokens: dict[str, int] | None = None
    schema_version: int = CURRENT_SCHEMA


@dataclass(kw_only=True)
class SessionRecord:
    # 2 adds ended_by, retrieval, brief_tokens, transcript_bytes; schema 1 reads them as None
    CURRENT_SCHEMA: ClassVar[int] = 2
    session_id: str
    author: str
    agent: str = "claude-code"
    branch: str | None = None
    worktree: str | None = None
    head_start: str | None = None
    head_end: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    files_touched: list[str] = field(default_factory=list)
    subagents: list[str] = field(default_factory=list)
    turns: list[TurnRecord] = field(default_factory=list)
    tool_calls: dict[str, int] = field(default_factory=dict)
    ctx_calls: dict[str, int] = field(default_factory=dict)
    tokens: dict[str, int] | None = None
    ended_by: str | None = None  # "session-end" or "idle"
    retrieval: bool | None = None  # whether the brief and `ctx find` were on
    brief_tokens: int | None = None
    # "source", "stored", and stored bytes per entry kind
    transcript_bytes: dict[str, int] | None = None
    schema_version: int = CURRENT_SCHEMA


@dataclass(kw_only=True)
class CheckpointRecord:
    CURRENT_SCHEMA: ClassVar[int] = 1
    sha: str
    author: str
    session_ids: list[str]
    created_at: str | None = None
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    rewritten_from: str | None = None
    schema_version: int = CURRENT_SCHEMA


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows refuses to replace a file another process has open.
                if attempt == 4:
                    raise
                time.sleep(0.01)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _from_dict(cls, data):
    if not isinstance(data, dict):
        raise RecordError(f"{cls.__name__}: not a JSON object")
    version = data.get("schema_version")
    # Every version up to the current one is readable; upgrades of older shapes go here.
    if type(version) is not int or not 1 <= version <= cls.CURRENT_SCHEMA:
        raise RecordError(f"{cls.__name__}: unsupported schema_version {version!r}")
    known = {f.name for f in fields(cls)}
    kwargs = {k: v for k, v in data.items() if k in known}
    if cls is SessionRecord:
        kwargs["turns"] = [_from_dict(TurnRecord, t) for t in kwargs.get("turns", [])]
    try:
        return cls(**kwargs)
    except TypeError as e:
        raise RecordError(f"{cls.__name__}: {e}") from None


def write_json(path: Path, value, config: Config, **dumps_kwargs) -> Path:
    """The one writer for JSON in the store: redacts every string, then writes atomically."""
    text = json.dumps(redact_value(value, config), sort_keys=True, **dumps_kwargs)
    atomic_write(path, text.encode("utf-8") + b"\n")
    return path


def write_record(path: Path, rec, config: Config) -> Path:
    # A record read in an earlier schema is written in the current one: it has every field.
    return write_json(path, asdict(rec) | {"schema_version": rec.CURRENT_SCHEMA}, config,
                      ensure_ascii=False, indent=2)


def read_record[R](path: Path, cls: type[R]) -> R:
    try:
        data = json.loads(path.read_bytes())
    except json.JSONDecodeError as e:
        raise RecordError(f"{path}: {e}") from None
    return _from_dict(cls, data)


def _named(directory: Path, name: str) -> Path:
    if not is_safe_name(name):
        raise RecordError(f"unsafe record name: {name!r}")
    return directory / f"{name}.json"


def session_path(store: StorePaths, session_id: str) -> Path:
    return _named(store.records / "sessions", session_id)


def checkpoint_path(store: StorePaths, sha: str) -> Path:
    return _named(store.records / "checkpoints", sha)


def write_session(store: StorePaths, rec: SessionRecord, config: Config) -> Path:
    return write_record(session_path(store, rec.session_id), rec, config)


def read_session(store: StorePaths, session_id: str) -> SessionRecord:
    return read_record(session_path(store, session_id), SessionRecord)


def write_checkpoint(store: StorePaths, rec: CheckpointRecord, config: Config) -> Path:
    return write_record(checkpoint_path(store, rec.sha), rec, config)


def read_checkpoint(store: StorePaths, sha: str) -> CheckpointRecord:
    return read_record(checkpoint_path(store, sha), CheckpointRecord)
