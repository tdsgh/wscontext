"""Host-local state files shared by the hook handlers and the worker. Stdlib only."""

import json
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from wscontext.config import Config
from wscontext.gitutil import GitError, git
from wscontext.redact.redactor import redact_text
from wscontext.store.locks import FileLock
from wscontext.store.paths import StorePaths, is_safe_name
from wscontext.store.records import write_json

SWEEP_EVERY_SECONDS = 86400


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


def parse_time(value) -> float:
    """An ISO timestamp as seconds since the epoch; 0 when it is not one."""
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


def last_activity(store: StorePaths, session_id: str, started) -> float:
    """When a session last did something: its start in `active.json`, or the last write of
    its turn state, which every prompt and tool call rewrites."""
    last = parse_time(started)
    try:
        last = max(last, turn_state_path(store, session_id).stat().st_mtime)
    except OSError:
        pass
    return last


def idle_sessions(store: StorePaths, config: Config) -> list[tuple[str, str]]:
    """Active sessions in any worktree idle past the staleness window, as
    (worktree key, session ID)."""
    cutoff = time.time() - config.capture.stale_session_minutes * 60
    idle = []
    for key, sessions in read_json(active_path(store)).items():
        for sid, started in sessions.items() if isinstance(sessions, dict) else ():
            if is_safe_name(sid) and last_activity(store, sid, started) < cutoff:
                idle.append((key, sid))
    return idle


def sweep_due(store: StorePaths, config: Config) -> bool:
    """Whether to spawn a `sweep`: the last one is over a day old, or a session is idle."""
    try:
        if time.time() - float((store.state / "last_sweep").read_text()) < SWEEP_EVERY_SECONDS:
            return bool(idle_sessions(store, config))
    except (OSError, ValueError):
        pass
    return True


def git_value(store: StorePaths, *args: str) -> str | None:
    try:
        return git(*args, cwd=store.root).strip() or None
    except (GitError, OSError):
        return None


def git_author(store: StorePaths) -> str:
    return (git_value(store, "config", "user.email") or git_value(store, "config", "user.name")
            or "unknown")
