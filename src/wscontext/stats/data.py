"""What was collected: counts, linked-commit share, sizes, redaction hits and sync status.

Also the shared log reader and percentile used by the call and hook views.
"""

import json
import math
from datetime import datetime
from pathlib import Path

from wscontext.cli.data import list_sessions, parse_time
from wscontext.config import Config
from wscontext.gitutil import GitError, git
from wscontext.history import sync
from wscontext.history.branch import ls_blobs, read_tip
from wscontext.logs import HOOKS_LOG
from wscontext.store.paths import StorePaths
from wscontext.store.records import SessionRecord


def in_window(stamp: str | None, since: datetime | None) -> bool:
    return since is None or ((parsed := parse_time(stamp)) is not None and parsed >= since)


def window_sessions(store: StorePaths, since: datetime | None) -> list[SessionRecord]:
    return [r for r in list_sessions(store) if in_window(r.started_at, since)]


def read_log(store: StorePaths, name: str, since: datetime | None) -> list[dict]:
    """Rows of a host log inside the window, oldest first: the rotated generation, then the live one."""
    rows = []
    for path in (store.logs / f"{name}.1", store.logs / name):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and in_window(row.get("ts"), since):
                rows.append(row)
    return rows


def percentile(values: list[float], p: int) -> float | None:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def _dir_bytes(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total


def _commits(store: StorePaths, since: datetime | None) -> dict:
    args = ["rev-list", "HEAD"] + ([f"--since={since.isoformat()}"] if since else [])
    try:
        shas = git(*args, cwd=store.root).split()
    except GitError:  # no commits yet
        shas = []
    checkpointed = {p.stem for p in (store.records / "checkpoints").glob("*.json")}
    linked = sum(sha in checkpointed for sha in shas)
    return {"commits": len(shas), "linked": linked,
            "linked_share": linked / len(shas) if shas else None}


def _branch_bytes(store: StorePaths) -> int:
    try:
        tip = read_tip(store)
        return sum(size for _, _, size in ls_blobs(store, tip).values()) if tip else 0
    except GitError:
        return 0


def _sync(store: StorePaths, config: Config) -> dict:
    if config.sync.remote is None:
        return {"sharing": False, "remote": None, "ahead": 0, "behind": 0, "pending_sessions": 0}
    try:
        status = sync.status(store, config)
        waiting = len(sync.pending(store, config))
    except GitError:
        return {"sharing": True, "remote": config.sync.remote, "ahead": None, "behind": None,
                "pending_sessions": None}
    return {"sharing": True, "remote": status.remote, "ahead": status.ahead,
            "behind": status.behind, "pending_sessions": waiting}


def collect(store: StorePaths, config: Config, since: datetime | None) -> dict:
    sessions = window_sessions(store, since)
    events = 0
    for rec in sessions:
        for raw in (store.raw / "sessions").glob(f"????-??-??-{rec.session_id}.jsonl"):
            try:
                events += raw.read_bytes().count(b"\n")
            except OSError:
                pass
    # Hits are counted by the raw copy and recorded on each worker run's hook-log line.
    copies = [r for r in read_log(store, HOOKS_LOG, since) if "redactions" in r]
    return {"sessions": len(sessions), "turns": sum(len(r.turns) for r in sessions),
            "transcript_events": events, **_commits(store, since),
            "store_bytes": _dir_bytes(store.raw) + _dir_bytes(store.records),
            "branch_bytes": _branch_bytes(store),
            "redaction_hits": sum(int(r["redactions"]) for r in copies),
            "transcript_copies": len(copies), "sync": _sync(store, config)}
