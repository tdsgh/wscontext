"""What was collected: counts, linked-commit share, sizes, redaction hits and sync status.

Also the shared log reader and percentile used by the call and hook views.
"""

import json
import math
import re
import tempfile
from datetime import datetime
from pathlib import Path

from wscontext.adapters.claude_code import measure
from wscontext.cli.data import list_sessions, parse_time
from wscontext.config import Config
from wscontext.gitutil import GitError, git
from wscontext.history import sync
from wscontext.history.branch import ls_blobs, read_blob, read_tip
from wscontext.index.db import IndexUnavailable, connect, db_path
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


_SESSION_PATH = re.compile(r"(?:raw/sessions/\d{4}-\d\d-\d\d-(?!pc\d{8}T\d{6}Z-)(.+)\.jsonl|raw/subagents/([^/]+)/.+"
                           r"|records/sessions/(.+)\.json)")
_SNAPSHOT = re.compile(r"raw/sessions/\d{4}-\d\d-\d\d-pc\d{8}T\d{6}Z-(.+)\.jsonl")
TOP = 3  # what a warning names: this many kinds, groups or sessions


def _group(path: str) -> str:
    """`raw/sessions`, `raw/snapshots` (PreCompact), `raw/subagents`, `records`: the top of a path."""
    if _SNAPSHOT.fullmatch(path):
        return "raw/snapshots"
    parts = path.split("/")
    return "/".join(parts[:2]) if parts[0] == "raw" and len(parts) > 2 else parts[0]


def _by_size(sizes: dict[str, int]) -> dict[str, int]:
    return dict(sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0])))


def _top(sizes: dict[str, int]) -> list[dict]:
    return [{"name": k, "bytes": v} for k, v in list(_by_size(sizes).items())[:TOP] if v]


def _branch_blobs(store: StorePaths, commit: str | None) -> dict[str, int]:
    try:
        return {p: v[2] for p, v in ls_blobs(store, commit).items()} if commit else {}
    except GitError:
        return {}


def _group_sizes(blobs: dict[str, int]) -> dict[str, int]:
    groups: dict[str, int] = {}
    for path, n in blobs.items():
        groups[_group(path)] = groups.get(_group(path), 0) + n
    return _by_size(groups)


def _snapshots(blobs: dict[str, int]) -> dict:
    """PreCompact snapshot files: bytes, file count and bytes per session."""
    by_session: dict[str, int] = {}
    for path, n in blobs.items():
        if m := _SNAPSHOT.fullmatch(path):
            by_session[m.group(1)] = by_session.get(m.group(1), 0) + n
    return {"bytes": sum(by_session.values()), "files": sum(bool(_SNAPSHOT.fullmatch(p)) for p in blobs),
            "by_session": by_session}


def _local_blobs(store: StorePaths) -> dict[str, int]:
    """Every file of the local store that history also holds, by branch-style path."""
    files = {}
    for root in (store.raw, store.records):
        for f in root.rglob("*"):
            try:
                if f.is_file():
                    files[f.relative_to(store.local).as_posix()] = f.stat().st_size
            except OSError:
                continue
    return files


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _stored_kinds(store: StorePaths, rec: SessionRecord, branch: dict[str, int]) -> dict:
    """Bytes by kind of one session: the record's own figures, else measured on the stored copy
    (schema-1 records), from `raw/` or, when pruned, from the history branch."""
    recorded = rec.transcript_bytes
    if recorded is not None:
        kinds = {k: v for k, v in recorded.items() if k not in ("stored", "source")}
        return {"kinds": kinds, "stored": recorded.get("stored", sum(kinds.values())),
                "source": recorded.get("source"), "measured": False}
    sid = rec.session_id
    main = sorted((store.raw / "sessions").glob(f"????-??-??-{sid}.jsonl"))
    sizes: dict[str, int] = {}
    if main:
        sizes = measure(main[-1])
    else:
        name = re.compile(rf"raw/sessions/\d{{4}}-\d\d-\d\d-{re.escape(sid)}\.jsonl")
        paths = sorted(p for p in branch if name.fullmatch(p))
        data = read_blob(store, paths[-1]) if paths else None
        if data is not None:
            with tempfile.TemporaryDirectory() as tmp:
                copy = Path(tmp) / "t.jsonl"
                copy.write_bytes(data)
                sizes = measure(copy)
    kinds = {k: v for k, v in sizes.items() if " " not in k and "." not in k}
    local_sub = [f.stat().st_size for f in (store.raw / "subagents" / sid).glob("*.jsonl")]
    sub = sum(local_sub) if local_sub else sum(
        n for p, n in branch.items() if p.startswith(f"raw/subagents/{sid}/"))
    if sub:
        kinds["subagent"] = kinds.get("subagent", 0) + sub
    return {"kinds": kinds, "stored": sum(kinds.values()), "source": None, "measured": True}


def session_volume(store: StorePaths, rec: SessionRecord) -> dict:
    """Bytes by kind of one session; reads the history branch only to measure a pruned schema-1 copy."""
    branch = _branch_blobs(store, read_tip(store))
    snapshots = _snapshots(_local_blobs(store))["by_session"]
    return {"session_id": rec.session_id, **_stored_kinds(store, rec, branch),
            "snapshot_bytes": snapshots.get(rec.session_id)
            or _snapshots(branch)["by_session"].get(rec.session_id, 0)}


def week_of(stamp: str | None) -> str | None:
    parsed = parse_time(stamp)
    if parsed is None:
        return None
    year, week, _ = parsed.isocalendar()
    return f"{year}-W{week:02d}"


def _history_growth(store: StorePaths, tip: str | None, since: datetime | None) -> list[dict]:
    """History branch size at the end of each week that saw a commit, newest 26 weeks."""
    if tip is None:
        return []
    try:
        out = git("log", "--format=%H %cI", tip, cwd=store.root)
    except GitError:
        return []
    last: dict[str, str] = {}  # newest commit of each week; the log runs newest first
    for line in out.splitlines():
        sha, _, stamp = line.partition(" ")
        if in_window(stamp, since) and (week := week_of(stamp)):
            last.setdefault(week, sha)
    rows = []
    for week in sorted(last)[-26:]:
        rows.append({"week": week, "bytes": sum(_branch_blobs(store, last[week]).values())})
    for prev, row in zip([None, *rows], rows):
        row["growth"] = row["bytes"] - (prev["bytes"] if prev else 0)
    return rows


def _pruned(store: StorePaths) -> int:
    try:
        return len(json.loads((store.state / "pruned.json").read_bytes()))
    except (OSError, ValueError):
        return 0


def _warnings(config: Config, sessions: list[dict], local: dict, shared: dict) -> list[dict]:
    limits = config.store
    kinds: dict[str, int] = {"snapshot": local["snapshots"]["bytes"]}
    for s in sessions:
        for k, n in s["kinds"].items():
            kinds[k] = kinds.get(k, 0) + n
    found = []
    for s in sessions:
        if s["stored"] > limits.session_warn_bytes:
            found.append({"scope": "session", "id": s["session_id"], "bytes": s["stored"],
                          "limit": limits.session_warn_bytes, "largest": _top(s["kinds"])})
    for scope, figures in (("local store", local), ("shared history", shared)):
        if figures["bytes"] > limits.store_warn_bytes:
            found.append({"scope": scope, "id": None, "bytes": figures["bytes"],
                          "limit": limits.store_warn_bytes, "largest": _top(figures["groups"]),
                          "largest_sessions": _top(figures["sessions"]),
                          "largest_kinds": _top(kinds) if scope == "local store" else []})
    return found


def _index(store: StorePaths) -> dict:
    """Index size, rows and lag, read-only: no refresh runs. Lag counts sessions and commits whose
    record, transcript copies or checkpoint differ in size from what the index last read."""
    try:
        conn = connect(store, readonly=True)
    except (IndexUnavailable, OSError):
        return {"present": False, "bytes": 0, "sessions": 0, "commits": 0, "documents": 0, "lag": None}
    try:
        size = sum(f.stat().st_size for f in (db_path(store), Path(f"{db_path(store)}-wal"))
                   if f.is_file())
        known = {path: n for path, n in conn.execute("SELECT path, size FROM sources")}
        sessions = {r[0] for r in conn.execute("SELECT session_id FROM sessions")}
        commits = {r[0] for r in conn.execute("SELECT sha FROM commits")}
        documents = conn.execute("SELECT count(*) FROM docs_fts").fetchone()[0]
    except Exception:
        return {"present": False, "bytes": 0, "sessions": 0, "commits": 0, "documents": 0, "lag": None}
    finally:
        conn.close()
    behind: set[str] = set()
    on_disk = {p.stem for p in (store.records / "sessions").glob("*.json")}
    for sid in on_disk:
        keys = [f"records/sessions/{sid}.json",
                *(f"raw/sessions/{p.name}" for p in (store.raw / "sessions").glob(f"????-??-??-{sid}.jsonl")),
                *(f"raw/subagents/{sid}/{p.name}" for p in (store.raw / "subagents" / sid).glob("*.jsonl"))]
        if sid not in sessions or any(
                known.get(k) != _size(store.local / k) for k in keys):
            behind.add(sid)
    cp_dir = store.records / "checkpoints"
    cp_on_disk = {p.stem for p in cp_dir.glob("*.json")} if cp_dir.is_dir() else set()
    lag_commits = {sha for sha in cp_on_disk
                   if sha not in commits
                   or known.get(f"records/checkpoints/{sha}.json") != _size(cp_dir / f"{sha}.json")}
    gone = (sessions - on_disk) | (commits - cp_on_disk)
    return {"present": True, "bytes": size, "sessions": len(sessions), "commits": len(commits),
            "documents": documents, "lag": len(behind) + len(lag_commits) + len(gone)}


def _public(fig: dict) -> dict:
    snap = fig["snapshots"]
    return {"bytes": fig["bytes"], "groups": fig["groups"],
            "snapshots": {"bytes": snap["bytes"], "files": snap["files"]}}


def _volume(store: StorePaths, config: Config, since: datetime | None,
            window: list[SessionRecord]) -> dict:
    tip = read_tip(store)
    branch = _branch_blobs(store, tip)
    shared: dict[str, int] = {}
    try:
        for t in sync.shared_tips(store):
            for path, n in _branch_blobs(store, t).items():
                shared[path] = max(shared.get(path, 0), n)
    except GitError:
        pass
    local = _local_blobs(store)

    def by_session(blobs: dict[str, int]) -> dict[str, int]:
        sessions: dict[str, int] = {}
        for path, n in blobs.items():
            if m := _SNAPSHOT.fullmatch(path) or _SESSION_PATH.fullmatch(path):
                sid = next(g for g in m.groups() if g)
                sessions[sid] = sessions.get(sid, 0) + n
        return sessions

    def figures_of(blobs: dict[str, int]) -> dict:
        return {"bytes": sum(blobs.values()), "groups": _group_sizes(blobs),
                "sessions": by_session(blobs), "snapshots": _snapshots(blobs)}

    local_fig, branch_fig, shared_fig = figures_of(local), figures_of(branch), figures_of(shared)
    snap_local, snap_branch = local_fig["snapshots"]["by_session"], branch_fig["snapshots"]["by_session"]
    all_volumes = {r.session_id: {"session_id": r.session_id, **_stored_kinds(store, r, branch),
                                  "snapshot_bytes": snap_local.get(r.session_id)
                                  or snap_branch.get(r.session_id, 0)}
                   for r in list_sessions(store)}
    windowed = [all_volumes[r.session_id] for r in window]

    kinds: dict[str, int] = {}
    for v in windowed:
        for k, n in v["kinds"].items():
            kinds[k] = kinds.get(k, 0) + n
    stored = sum(v["stored"] for v in windowed)
    known_source = [v for v in windowed if v["source"] is not None]
    weeks: dict[str, dict] = {}
    for rec in window:
        if week := week_of(rec.started_at):
            row = weeks.setdefault(week, {"week": week, "sessions": 0, "stored_bytes": 0})
            row["sessions"] += 1
            row["stored_bytes"] += all_volumes[rec.session_id]["stored"]
    with_retrieval = [r for r in window if r.retrieval is not None]
    return {
        "store_bytes": local_fig["bytes"], "branch_bytes": branch_fig["bytes"],
        "local": _public(local_fig), "history_branch": _public(branch_fig),
        "shared_history": _public(shared_fig),
        "pruned_paths": _pruned(store),
        "transcript": {"stored": stored, "kinds": _by_size(kinds),
                       "source": sum(v["source"] for v in known_source),
                       "stored_of_known_source": sum(v["stored"] for v in known_source),
                       "sessions_with_source": len(known_source),
                       "sessions_measured": sum(v["measured"] for v in windowed)},
        "per_session": sorted(windowed, key=lambda v: -v["stored"])[:TOP * 3],
        "growth": {"history_branch": _history_growth(store, tip, since),
                   "sessions": [weeks[w] for w in sorted(weeks)]},
        "warnings": _warnings(config, list(all_volumes.values()), local_fig, shared_fig),
        "limits": {"session_warn_bytes": config.store.session_warn_bytes,
                   "store_warn_bytes": config.store.store_warn_bytes},
        "index": _index(store),
        "retrieval": {"enabled": config.retrieval.enabled, "brief": config.retrieval.brief,
                      "sessions_with_switch": len(with_retrieval),
                      "sessions_retrieval_on": sum(bool(r.retrieval) for r in with_retrieval),
                      "sessions_with_brief": sum(bool(r.brief_tokens) for r in with_retrieval),
                      "brief_share": (sum(bool(r.brief_tokens) for r in with_retrieval)
                                      / len(with_retrieval)) if with_retrieval else None,
                      "brief_tokens": sum(r.brief_tokens or 0 for r in window)},
    }


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
            **_volume(store, config, since, sessions),
            "redaction_hits": sum(int(r["redactions"]) for r in copies),
            "transcript_copies": len(copies), "sync": _sync(store, config)}
