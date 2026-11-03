"""Sharing the history branch through the sync remote.

Nothing here runs unless `[sync] remote` is set. Pushes happen only when a
caller asks; nothing in this module pushes on its own.
"""

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from wscontext.config import Config
from wscontext.gitutil import GitError, git, git_bytes
from wscontext.history.branch import (HISTORY_REF, local_path, ls_blobs,
                                      merge_into_local, read_tip)
from wscontext.store.paths import StorePaths
from wscontext.store.records import atomic_write

log = logging.getLogger(__name__)

MATERIALIZE_BATCH_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class SyncStatus:
    remote: str | None
    ahead: int
    behind: int


def tracking_ref(remote: str) -> str:
    return f"refs/remotes/{remote}/wscontext/history"


def fetch(store: StorePaths, config: Config, force: bool = False) -> bool:
    """Fetch the remote history, union-merge it into the local ref and materialize it.

    Throttled through `state/last_fetch` unless forced. Returns True when local
    history changed. Raises GitError when the fetch itself fails.
    """
    remote = config.sync.remote
    if remote is None:
        return False
    stamp = store.state / "last_fetch"
    now = time.time()
    if not force:
        try:
            if now - float(stamp.read_text()) < config.sync.fetch_throttle_seconds:
                return False
        except (OSError, ValueError):
            pass
    atomic_write(stamp, f"{now}\n".encode())
    # A glob refspec that matches nothing succeeds: a remote without history is not an error.
    git("fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
        remote, f"+{HISTORY_REF}*:{tracking_ref(remote)}*", cwd=store.root)
    theirs = read_tip(store, tracking_ref(remote))
    if theirs is None:
        return False
    before = read_tip(store)
    if merge_into_local(store, theirs) == before:
        return False
    materialize(store)
    return True


def push(store: StorePaths, config: Config) -> bool:
    """Push the local history; on a rejection fetch, merge and retry once.

    Never raises. Returns True when the remote holds the local tip.
    """
    remote = config.sync.remote
    if remote is None:
        return False
    try:
        for attempt in range(2):
            tip = read_tip(store)
            if tip is None:
                return False
            try:
                # --no-verify: this push runs from inside the user's own pre-push hook.
                git("push", "--quiet", "--no-verify", remote, f"{tip}:{HISTORY_REF}",
                    cwd=store.root)
            except GitError:
                if attempt == 1:
                    raise
                fetch(store, config, force=True)
                continue
            git("update-ref", tracking_ref(remote), tip, cwd=store.root)
            return True
    except Exception:
        log.warning("history push to %s failed", remote, exc_info=True)
    return False


def status(store: StorePaths, config: Config) -> SyncStatus:
    """Ahead/behind counts against the last-fetched remote history; does not fetch."""
    remote = config.sync.remote
    local = read_tip(store)
    theirs = read_tip(store, tracking_ref(remote)) if remote else None
    if local and theirs:
        out = git("rev-list", "--left-right", "--count", f"{local}...{theirs}", cwd=store.root)
        ahead, behind = map(int, out.split())
    else:
        ahead, behind = (int(git("rev-list", "--count", c, cwd=store.root)) if c else 0
                         for c in (local, theirs))
    return SyncStatus(remote, ahead, behind)


def pending(store: StorePaths, config: Config) -> list[str]:
    """Session IDs with files that the next push would send."""
    local = read_tip(store)
    if config.sync.remote is None or local is None:
        return []
    theirs = read_tip(store, tracking_ref(config.sync.remote))
    shared = {p: sha for p, (_, sha, _) in ls_blobs(store, theirs).items()} if theirs else {}
    ours = ls_blobs(store, local)
    sessions = [p.split("/")[2].removesuffix(".json") for p in ours
                if p.startswith("records/sessions/")]
    out = set()
    for path, (_, sha, _) in ours.items():
        if shared.get(path) == sha:
            continue
        parts = path.split("/")
        if parts[:2] == ["records", "sessions"]:
            out.add(parts[2].removesuffix(".json"))
        elif parts[:2] == ["raw", "subagents"]:
            out.add(parts[2])
        elif parts[:2] == ["raw", "sessions"]:
            out.update(s for s in sessions if parts[2].endswith(f"-{s}.jsonl"))
    return sorted(out)


def materialize(store: StorePaths) -> int:
    """Write branch files into `raw/` and `records/` where the local copy is missing or smaller.

    Returns the number of files written.
    """
    tip = read_tip(store)
    if tip is None:
        return 0
    wanted: list[tuple[Path, str, int]] = []
    for path, (_, sha, size) in ls_blobs(store, tip).items():
        target = local_path(store, path)
        if target is None:
            log.warning("history path not materialized: %s", path)
            continue
        try:
            have = target.stat().st_size
        except FileNotFoundError:
            have = -1
        if have < size:
            wanted.append((target, sha, size))
    # Blobs are read in bounded batches so a fresh clone never holds all history in memory.
    batches: list[list[tuple[Path, str, int]]] = [[]]
    total = 0
    for item in wanted:
        if batches[-1] and total + item[2] > MATERIALIZE_BATCH_BYTES:
            batches.append([])
            total = 0
        batches[-1].append(item)
        total += item[2]
    for batch in batches:
        if not batch:
            continue
        request = "".join(f"{sha}\n" for _, sha, _ in batch).encode()
        out = git_bytes("cat-file", "--batch", cwd=store.root, input=request)
        pos = 0
        for target, _, _ in batch:
            header_end = out.index(b"\n", pos)
            size = int(out[pos:header_end].split()[2])
            start = header_end + 1
            atomic_write(target, out[start:start + size])
            pos = start + size + 1
    return len(wanted)
