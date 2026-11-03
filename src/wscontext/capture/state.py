"""Host-local state files shared by the hook handlers and the worker. Stdlib only."""

import json
from collections.abc import Callable
from pathlib import Path

from wscontext.config import Config
from wscontext.gitutil import GitError, git
from wscontext.redact.redactor import redact_text
from wscontext.store.locks import FileLock
from wscontext.store.paths import StorePaths
from wscontext.store.records import write_json


def turn_state_path(store: StorePaths, session_id: str) -> Path:
    return store.state / "turns" / f"{session_id}.json"


def active_path(store: StorePaths) -> Path:
    return store.state / "active.json"


def worktree_key(store: StorePaths, config: Config) -> str:
    """The worktree path as it is stored in a state file's keys, i.e. redacted."""
    return redact_text(str(store.root), config)[0]


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def update_json(path: Path, change: Callable[[dict], None],
                config: Config) -> dict:
    """Read-modify-write a JSON object under a short lock; concurrent async hooks must not lose updates."""
    with FileLock(path.with_name(path.name + ".lock"), stale_after=10, timeout=1.0):
        data = read_json(path)
        change(data)
        write_json(path, data, config)
    return data


def git_value(store: StorePaths, *args: str) -> str | None:
    try:
        return git(*args, cwd=store.root).strip() or None
    except (GitError, OSError):
        return None


def git_author(store: StorePaths) -> str:
    return (git_value(store, "config", "user.email") or git_value(store, "config", "user.name")
            or "unknown")
