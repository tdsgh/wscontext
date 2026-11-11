"""The detached worker: transcript copy, redaction, parsing, record writing and history commits.

Run as `python -m wscontext.capture.worker <action> <session-id> <transcript> <cwd>`.
Actions: `fetch`, `copy`, `snapshot`, `final`, and `sweep`, which takes no session or
transcript: it finalizes idle sessions and prunes old `raw/` copies.
"""

import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from wscontext.adapters.claude_code import ClaudeCodeAdapter, measure, owner_ids, reduce
from wscontext.capture.kinds import tool_kind
from wscontext.capture.state import (active_path, git_author, git_value, idle_sessions,
                                     last_activity, parse_time, read_json, turn_state_path,
                                     update_json)
from wscontext.config import Config, load_config
from wscontext.gitutil import GitError, git
from wscontext.history import sync
from wscontext.history.branch import branch_path, commit_paths, ls_blobs, read_tip
from wscontext.index import build as index_build
from wscontext.logs import HOOKS_LOG, append_log, now_iso
from wscontext.redact.account import learn_account_file, learn_account_ids, with_account_ids
from wscontext.redact.rules import UUID, account_digest
from wscontext.redact.redactor import add_digests, redact_bytes
from wscontext.store.locks import FileLock, LockTimeout
from wscontext.store.paths import StorePaths, is_safe_name
from wscontext.store.records import (RecordError, SessionRecord, TurnRecord, atomic_write,
                                     read_session, write_session)

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
SWEEP_STALE_SECONDS = 600


def detach_kwargs(platform: str) -> dict:
    if platform == "win32":
        return {"creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def spawn_worker(args: list[str], cwd: Path, module: str = "wscontext.capture.worker") -> None:
    proc = subprocess.Popen([sys.executable, "-m", module, *args],
                            cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, **detach_kwargs(sys.platform))
    # Never waited for: without a status, collecting `proc` warns that the process still runs.
    proc.returncode = 0


def _raw_name(store: StorePaths, session_id: str, record: SessionRecord | None,
              snapshot: bool) -> Path:
    directory = store.raw / "sessions"
    if snapshot:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return directory / f"{now_iso()[:10]}-pc{stamp}-{session_id}.jsonl"
    existing = sorted(directory.glob(f"????-??-??-{session_id}.jsonl"))
    if existing:
        return existing[0]
    date = (record.started_at if record and record.started_at else now_iso())[:10]
    return directory / f"{date}-{session_id}.jsonl"


def copy_reduced(source: Path, target: Path, config: Config,
                 store: StorePaths | None = None) -> int:
    """Store a transcript: reduce it to the stored shape, redact, write. Returns the hit count.

    The account IDs on its `session-owner` lines are masked in this copy. With `store`, their
    digests are also kept in the store's local state for later writes."""
    data = source.read_bytes()
    ids = owner_ids(data)
    if ids:
        if store is not None:
            learn_account_ids(store, config, ids)
        config = add_digests(config, frozenset(account_digest(i) for i in ids if UUID.fullmatch(i)))
    cut = config.capture.tool_result_max_chars
    hits: list[str] = []
    if cut:
        # a cut must not leave the head of a secret the redactor no longer recognizes
        data, hits = redact_bytes(data, config)
    data, more = redact_bytes(reduce(data, cut), config)
    atomic_write(target, data)
    return len(hits) + len(more)


def stored_sizes(raw: Path, subagents: Path) -> dict[str, int]:
    """A session's stored bytes per kind and `stored` in all. Its subagent copies in
    `subagents` count as `subagent`, like sidechain lines."""
    sizes = {k: v for k, v in measure(raw).items() if " " not in k and "." not in k}
    sizes["subagent"] = sizes.get("subagent", 0) + sum(
        p.stat().st_size for p in subagents.glob("*.jsonl"))
    return {**sizes, "stored": sum(sizes.values())}


def _session_lock(store: StorePaths, session_id: str) -> FileLock:
    # Stop, SessionEnd and sweep workers can overlap; serialize them per session.
    return FileLock(store.state / "locks" / f"worker-{session_id}.lock",
                    stale_after=300, timeout=120)


def _read_record(store: StorePaths, session_id: str) -> SessionRecord | None:
    try:
        return read_session(store, session_id)
    except (OSError, RecordError):
        return None


def _final(store: StorePaths, config: Config, session_id: str, source: Path | None,
           raw: Path, ended_by: str, ended_at: str | None = None) -> None:
    """The final step of a session: subagent copies, the record and the history commit.

    Reads `source` when it still exists, else the raw copy. Hook-side counts add to the
    record's, so a session finalized and then resumed keeps its earlier counts."""
    copied: list[Path] = []
    config = with_account_ids(config, store)  # the copy just made may have taught new IDs
    record = _read_record(store, session_id) or SessionRecord(
        session_id=session_id, author=git_author(store), worktree=str(store.root),
        started_at=now_iso())
    state = read_json(turn_state_path(store, session_id))
    parsed = ClaudeCodeAdapter().parse(source or raw) if source or raw.is_file() else None
    source_bytes = source.stat().st_size if source else None
    turns: list[TurnRecord] = []
    if parsed and parsed.supported:
        for t in parsed.turns:
            kinds: dict[str, int] = {}
            for name, n in t.tool_calls.items():
                kind = tool_kind(name)
                kinds[kind] = kinds.get(kind, 0) + n
            turns.append(TurnRecord(session_id=session_id, author=record.author, index=t.index,
                                    started_at=t.started_at or "", ended_at=t.ended_at,
                                    tool_calls=kinds, tokens=t.tokens))
        for ref in parsed.subagents:
            if is_safe_name(ref.agent_id) and ref.path is not None:
                target = store.raw / "subagents" / session_id / f"{ref.agent_id}.jsonl"
                copy_reduced(ref.path, target, config, store)
                copied.append(target)
                if source_bytes is not None:
                    source_bytes += ref.path.stat().st_size
        config = with_account_ids(config, store)
        record.subagents = [r.agent_id for r in parsed.subagents if is_safe_name(r.agent_id)]
    else:
        # unknown format: only the hook-side turn times survive; a resumed session adds its own
        turns = list(record.turns)
        for t in state.get("turns", []):
            turns.append(TurnRecord(session_id=session_id, author=record.author,
                                    index=len(turns), started_at=t.get("started_at") or "",
                                    ended_at=t.get("ended_at")))
    token_totals: dict[str, int] = {}
    for t in turns:
        for key, value in (t.tokens or {}).items():
            token_totals[key] = token_totals.get(key, 0) + value
    record.turns = turns
    record.tokens = token_totals or None
    for name in ("tool_calls", "ctx_calls"):
        old, new = getattr(record, name), state.get(name, {})
        setattr(record, name, {k: old.get(k, 0) + new.get(k, 0) for k in old | new})
    record.files_touched = sorted(set(record.files_touched) | set(state.get("files", [])))
    record.ended_at = ended_at or now_iso()
    record.ended_by = ended_by
    if "retrieval" in state:
        record.retrieval = state["retrieval"]
    if "brief_tokens" in state:
        record.brief_tokens = (record.brief_tokens or 0) + state["brief_tokens"]
    record.head_end = git_value(store, "rev-parse", "HEAD")
    if raw.is_file():
        record.transcript_bytes = stored_sizes(raw, store.raw / "subagents" / session_id)
        if source_bytes is not None:
            record.transcript_bytes["source"] = source_bytes
    write_session(store, record, config)
    paths = [raw] if raw.is_file() else []
    commit_paths(store, [*paths, *copied, store.records / "sessions" / f"{session_id}.json"],
                 f"wscontext: session {session_id}")
    turn_state_path(store, session_id).unlink(missing_ok=True)


def _finalize_idle(store: StorePaths, config: Config) -> tuple[list[str], list[str]]:
    """Finalize every active session, in any worktree, idle past the staleness window.

    Returns the finalized session IDs and one error per session that failed."""
    done: list[str] = []
    errors: list[str] = []
    cutoff = time.time() - config.capture.stale_session_minutes * 60
    for key, sid in idle_sessions(store, config):
        removed: list[float] = []

        def drop(d: dict) -> None:
            # checked again under the lock: a prompt since the first read keeps it active
            sessions = d.get(key)
            if isinstance(sessions, dict) and sid in sessions:
                last = last_activity(store, sid, sessions[sid])
                if last < cutoff:
                    del sessions[sid]
                    removed.append(last)
                    if not sessions:
                        del d[key]

        try:
            update_json(active_path(store), drop, config)
            if not removed:
                continue
            # the session's own worktree, for its HEAD; the current one when it is gone
            try:
                target = StorePaths.resolve(Path(key)) if Path(key).is_dir() else store
            except (GitError, OSError):
                target = store
            transcript = read_json(turn_state_path(store, sid)).get("transcript")
            source = Path(transcript) if isinstance(transcript, str) and transcript else None
            source = source if source and source.is_file() else None
            ended_at = datetime.fromtimestamp(removed[0], timezone.utc).isoformat(
                timespec="seconds")
            with _session_lock(store, sid):
                raw = _raw_name(store, sid, _read_record(store, sid), False)
                if source:
                    copy_reduced(source, raw, config, store)
                _final(target, config, sid, source, raw, "idle", ended_at)
            done.append(sid)
        except Exception as e:
            errors.append(f"{sid}: {type(e).__name__}: {e}")
    return done, errors


def _index(entry: dict, update: Callable[[], object]) -> None:
    """Keep the index current after a copy, a final step or a fetch, whether or not retrieval is
    on: the switch changes only what the agent sees. A failure is logged on the hook-run entry
    (visible to doctor and stats) and never undoes the work it follows."""
    try:
        update()
    except Exception as e:
        entry["ok"] = False
        entry.setdefault("error", f"index: {type(e).__name__}: {e}")


def _raw_files(store: StorePaths, session_id: str) -> list[Path]:
    sessions = store.raw / "sessions"
    found = [*sessions.glob(f"????-??-??-{session_id}.jsonl"),
             *sessions.glob(f"????-??-??-pc????????T??????Z-{session_id}.jsonl"),
             *(store.raw / "subagents" / session_id).glob("*.jsonl")]
    return [p for p in found if is_safe_name(p.name)]


def _prune(store: StorePaths, config: Config) -> list[str]:
    """Remove `raw/` copies of sessions started more than `retention.raw_days` ago whose
    blob is on the local history tip and, with sync on, on a shared tip.

    Each pruned branch path goes into `state/pruned.json` first, so materialize never
    writes it back. Never touches the history ref. Returns the pruned branch paths."""
    tip = read_tip(store)
    if tip is None:
        return []
    cutoff = time.time() - config.retention.raw_days * 86400
    active = {sid for sessions in read_json(active_path(store)).values()
              if isinstance(sessions, dict) for sid in sessions}
    candidates: list[Path] = []
    for path in sorted((store.records / "sessions").glob("*.json")):
        sid = path.stem
        if sid in active or not is_safe_name(sid):
            continue
        record = _read_record(store, sid)
        if record and 0 < parse_time(record.started_at) < cutoff:
            candidates += _raw_files(store, sid)
    if not candidates:
        return []
    shas = git("hash-object", "--no-filters", "--stdin-paths", cwd=store.root,
               input="".join(f"{p.resolve()}\n" for p in candidates)).split()
    on_tip = {path: sha for path, (_, sha, _) in ls_blobs(store, tip).items()}
    shared = None
    if config.sync.remote:
        shared = {(path, sha) for t in sync.shared_tips(store)
                  for path, (_, sha, _) in ls_blobs(store, t).items()}
    prune: dict[str, Path] = {}
    for path, sha in zip(candidates, shas):
        rel = branch_path(store, path)
        if on_tip.get(rel) == sha and (shared is None or (rel, sha) in shared):
            prune[rel] = path
    if not prune:
        return []
    stamp = now_iso()
    update_json(store.state / "pruned.json", lambda d: d.update(dict.fromkeys(prune, stamp)),
                config)
    for path in prune.values():
        path.unlink(missing_ok=True)
        if path.parent.parent.name == "subagents":
            try:
                path.parent.rmdir()
            except OSError:
                pass  # other copies are still there
    return sorted(prune)


def sweep(store: StorePaths, config: Config) -> dict:
    """Finalize idle sessions, then prune old `raw/` copies. Skips when another sweep runs."""
    lock = FileLock(store.state / "locks" / "sweep.lock", stale_after=SWEEP_STALE_SECONDS,
                    timeout=0)
    try:
        lock.__enter__()
    except LockTimeout:
        return {"skipped": "another sweep is running"}
    try:
        atomic_write(store.state / "last_sweep", f"{time.time()}\n".encode())
        finalized, errors = _finalize_idle(store, config)
        out: dict = {"finalized": finalized, "pruned": _prune(store, config)}
        if errors:
            out["errors"] = errors
        return out
    finally:
        lock.__exit__(None, None, None)


def run(action: str, session_id: str, transcript: str, cwd: str) -> None:
    store = StorePaths.resolve(Path(cwd))
    config = load_config(store.root)
    started = time.monotonic()
    entry = {"ts": now_iso(), "event": f"worker:{action}", "session_id": session_id, "ok": True}
    try:
        learn_account_file(store, config)
        config = with_account_ids(config, store)
        if action == "fetch":
            entry["changed"] = sync.fetch(store, config)
            if entry["changed"]:
                _index(entry, lambda: index_build.refresh(store))
        elif action == "sweep":
            entry.update(sweep(store, config))
            for sid in entry.get("finalized", []):
                _index(entry, lambda: index_build.index_session(store, sid))
            if entry.get("errors"):
                entry["ok"] = False
            elif not entry.get("finalized") and not entry.get("pruned"):
                return  # a sweep that found nothing to do is not logged
        elif action in {"copy", "snapshot", "final"}:
            if not is_safe_name(session_id):
                raise ValueError("unsafe session id")
            source = Path(transcript)
            with _session_lock(store, session_id):
                raw = _raw_name(store, session_id, _read_record(store, session_id),
                                action == "snapshot")
                if action != "final" or source.is_file():
                    entry["redactions"] = copy_reduced(source, raw, config, store)
                if action == "final":
                    # the final step runs from the raw copy when the transcript is gone
                    _final(store, config, session_id, source if source.is_file() else None,
                           raw, "session-end")
            _index(entry, lambda: index_build.index_session(store, session_id))
        else:
            raise ValueError(f"unknown worker action {action!r}")
    except Exception as e:
        entry["ok"] = False
        entry["error"] = f"{type(e).__name__}: {e}"
    entry["ms"] = round((time.monotonic() - started) * 1000)
    try:
        append_log(store, with_account_ids(config, store), HOOKS_LOG, entry)
    except Exception:
        pass


def main(argv: list[str]) -> int:
    try:
        run(*argv[:4])
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
