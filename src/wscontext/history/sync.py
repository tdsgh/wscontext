"""Sharing the history ref through the sync remote.

Nothing here runs unless `[sync] remote` is set. Pushes happen only when a
caller asks; nothing in this module pushes on its own.
"""

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from wscontext.config import Config
from wscontext.gitutil import GitError, git, git_bytes
from wscontext.history.branch import (HISTORY_REF, LEGACY_REF, is_ancestor, local_path,
                                      ls_blobs, merge_into_local, migrate_legacy, read_tip)
from wscontext.redact.redactor import redact_bytes
from wscontext.store.paths import StorePaths
from wscontext.store.records import atomic_write

log = logging.getLogger(__name__)

MATERIALIZE_BATCH_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class SyncStatus:
    remote: str | None
    ahead: int
    behind: int


# Tracking refs sit outside `refs/remotes/`: a plain `git fetch --prune` never deletes
# them and the default `refs/heads/*` mapping never overwrites them.
def tracking_ref(remote: str) -> str:
    return f"refs/wscontext/remotes/{remote}/history"


def legacy_tracking_ref(remote: str) -> str:
    """Where a fetch puts the remote's stage-1 `LEGACY_REF` branch."""
    return f"refs/wscontext/remotes/{remote}/legacy-history"


def _drop_old_tracking(store: StorePaths, remote: str) -> None:
    """Delete the tracking refs earlier builds kept under `refs/remotes/<remote>/wscontext/`.

    Runs only while neither new tracking ref exists, i.e. once: a ref a plain `git fetch`
    later recreates from a stage-1 teammate's branch is git's own. Never read as shared.
    Each delete is compare-and-swap on the tip read.
    """
    if git("for-each-ref", "--count=1", tracking_ref(remote), legacy_tracking_ref(remote),
           cwd=store.root).strip():
        return
    out = git("for-each-ref", "--format=%(refname) %(objectname)",
              f"refs/remotes/{remote}/wscontext/history",
              f"refs/remotes/{remote}/wscontext/legacy-history", cwd=store.root)
    for line in out.splitlines():
        ref, old = line.split()
        try:
            git("update-ref", "-d", ref, old, cwd=store.root)
            log.info("removed the old history tracking ref %s", ref)
        except GitError as e:
            log.debug("old tracking ref CAS delete failed: %s", e)


def fetch(store: StorePaths, config: Config, force: bool = False) -> bool:
    """Fetch the remote history, union-merge it into the local ref and materialize it.

    Reads both remote locations: `HISTORY_REF` and the stage-1 `LEGACY_REF` branch.

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
    _drop_old_tracking(store, remote)
    # A glob refspec that matches nothing succeeds: a remote without history is not an error.
    # --prune drops the tracking ref of a remote ref that is gone, so no push leases on it.
    # --refmap= stops the configured `refs/heads/*` refspec from recreating the old
    # `refs/remotes/<remote>/wscontext/history` from the legacy branch.
    git("fetch", "--quiet", "--prune", "--refmap=", "--no-tags", "--no-write-fetch-head",
        "--no-recurse-submodules", remote, f"+{HISTORY_REF}*:{tracking_ref(remote)}*",
        f"+{LEGACY_REF}*:{legacy_tracking_ref(remote)}*", cwd=store.root)
    before = read_tip(store)
    for ref in (tracking_ref(remote), legacy_tracking_ref(remote)):
        theirs = read_tip(store, ref)
        if theirs is not None:
            merge_into_local(store, theirs)
    if read_tip(store) == before:
        return False
    materialize(store, config)
    return True


def push(store: StorePaths, config: Config) -> bool:
    """Push the local history; on a rejection fetch, merge and retry once.

    When the last fetch saw the remote's stage-1 `LEGACY_REF` branch and the local tip
    contains it, the same atomic push deletes that branch, leased on the fetched tip.
    Never raises. Returns True when the remote holds the local tip.
    """
    remote = config.sync.remote
    if remote is None:
        return False
    try:
        migrate_legacy(store)
        _drop_old_tracking(store, remote)
        for attempt in range(2):
            tip = read_tip(store)
            if tip is None:
                return False
            legacy = read_tip(store, legacy_tracking_ref(remote))
            if legacy is not None and not is_ancestor(store, legacy, tip):
                legacy = None
            # --no-verify: this push runs from inside the user's own pre-push hook.
            args = ["push", "--quiet", "--no-verify"]
            if legacy:
                args += ["--atomic", f"--force-with-lease={LEGACY_REF}:{legacy}"]
            args += [remote, f"{tip}:{HISTORY_REF}"] + ([f":{LEGACY_REF}"] if legacy else [])
            try:
                git(*args, cwd=store.root)
            except GitError:
                if attempt == 1:
                    raise
                fetch(store, config, force=True)
                continue
            git("update-ref", tracking_ref(remote), tip, cwd=store.root)
            if legacy:
                git("update-ref", "-d", legacy_tracking_ref(remote), legacy, cwd=store.root)
                log.info("removed the stage-1 history branch %s from %s", LEGACY_REF, remote)
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


def materialize(store: StorePaths, config: Config) -> int:
    """Copy branch files into `raw/` and `records/`, redacted with the local rules.

    Each blob is considered once: `state/materialized.json` maps branch paths to the
    blob last considered, and only a new blob or a missing local file is looked at
    again. A considered blob is written when the local file is missing or shorter
    than the redacted blob; a local copy at least as long is this host's own,
    newer capture or already current. Paths in `state/pruned.json` (a JSON list of
    branch paths, or an object keyed by them) were pruned on purpose and are never
    written. Returns the number of files written.
    """
    tip = read_tip(store)
    if tip is None:
        return 0
    seen_file = store.state / "materialized.json"
    try:
        seen = json.loads(seen_file.read_bytes())
    except (OSError, ValueError):
        seen = {}
    if not isinstance(seen, dict):
        seen = {}
    try:
        pruned = {p for p in json.loads((store.state / "pruned.json").read_bytes())
                  if isinstance(p, str)}
    except (OSError, ValueError, TypeError):
        pruned = set()
    current: dict[str, str] = {}
    wanted: list[tuple[Path, str, int]] = []
    for path, (_, sha, size) in ls_blobs(store, tip).items():
        target = local_path(store, path)
        if target is None:
            log.warning("history path not materialized: %s", path)
            continue
        current[path] = sha
        if path in pruned:
            continue
        if seen.get(path) != sha or not target.exists():
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
    written = 0
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
            data, hits = redact_bytes(out[start:start + size], config)
            pos = start + size + 1
            if hits:
                log.warning("local rules redacted shared history %s: %s", target.name,
                            ", ".join(sorted(set(hits))))
            try:
                have = target.stat().st_size
            except FileNotFoundError:
                have = -1
            if len(data) > have:
                atomic_write(target, data)
                written += 1
    if current != seen:
        atomic_write(seen_file, json.dumps(current, sort_keys=True).encode() + b"\n")
    return written


def shared_tips(store: StorePaths) -> list[str]:
    """Tips of every remote-tracking history ref, legacy ones too, as of the last fetch or push."""
    return git("for-each-ref", "--format=%(objectname)", tracking_ref("*"),
               legacy_tracking_ref("*"), cwd=store.root).split()


def shared_paths(store: StorePaths) -> set[str]:
    """Branch paths present on any remote-tracking history ref."""
    return {path for tip in shared_tips(store) for path in ls_blobs(store, tip)}
