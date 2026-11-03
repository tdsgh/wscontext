"""`ctx forget <session>`: remove a session and its links from the store and the unshared history."""

import dataclasses
import json
import re
import shutil

from wscontext.capture.state import active_path, read_json, turn_state_path, update_json
from wscontext.cli.output import Reply, error
from wscontext.config import Config
from wscontext.history.branch import (commit_paths, ls_blobs, read_tip, remove_paths,
                                      squash_unshared)
from wscontext.history.sync import shared_paths, shared_tips
from wscontext.store.locks import FileLock, LockTimeout
from wscontext.store.paths import StorePaths, is_safe_name
from wscontext.store.records import RecordError, read_checkpoint, write_checkpoint

TRAILER_NOTE = ("Commits made in this session keep their AI-Session trailer: Git history is not "
                "rewritten. The trailer is only an ID and no longer resolves to anything.")


def add_arguments(parser) -> None:
    parser.add_argument("session", help="the exact session ID")
    parser.add_argument("--dry-run", action="store_true", help="list what would be removed")


def session_paths(store: StorePaths, sid: str) -> list[str]:
    """Branch-style paths of the session's files, found in the local store and on the local branch.

    A session's subagent transcripts are one path: its directory.
    """
    raw = re.compile(rf"raw/sessions/\d{{4}}-\d{{2}}-\d{{2}}-(?:pc\d{{8}}T\d{{6}}Z-)?{re.escape(sid)}\.jsonl")
    names = {p.relative_to(store.local).as_posix() for p in store.local.glob("*/*/*")}
    tip = read_tip(store)
    if tip:
        names |= set(ls_blobs(store, tip))
    found = set()
    for name in names:
        if name == f"records/sessions/{sid}.json" or raw.fullmatch(name):
            found.add(name)
        elif name.startswith(f"raw/subagents/{sid}/") or name == f"raw/subagents/{sid}":
            found.add(f"raw/subagents/{sid}")
    return sorted(found)


def _under(path: str, shared: set[str]) -> bool:
    return path in shared or any(s.startswith(path + "/") for s in shared)


def run(args, store: StorePaths, config: Config) -> Reply:
    sid = args.session
    if not is_safe_name(sid):
        return error(f"{sid!r} is not a session ID", args.json)
    paths = session_paths(store, sid)
    if not paths:
        return error(f"no session {sid!r}", args.json)
    if any(sid in sessions for sessions in read_json(active_path(store)).values()
           if isinstance(sessions, dict)):
        return error(f"session {sid} is still active; end it first", args.json)
    shared = shared_paths(store)
    pushed = [p for p in paths if _under(p, shared)]
    if pushed:
        return error(
            f"session {sid} is already on the shared history ({len(pushed)} of its "
            f"{len(paths)} paths are on a remote). Forgetting it needs an explicit rewrite of "
            "the shared wscontext/history branch and a force-push, and every teammate has to "
            "re-fetch it. ctx forget does not do that.", args.json, shared=True, paths=pushed)
    links = []
    for path in sorted((store.records / "checkpoints").glob("*.json")):
        try:
            rec = read_checkpoint(store, path.stem)
        except (OSError, RecordError):
            continue
        if sid in rec.session_ids:
            links.append(rec)
    dropped = [r.sha for r in links if r.session_ids == [sid]]
    trimmed = [r.sha for r in links if r.session_ids != [sid]]
    result = {"forgotten": not args.dry_run, "dry_run": args.dry_run, "session_id": sid,
              "removed": paths, "checkpoints_removed": dropped, "checkpoints_updated": trimmed,
              "notes": [TRAILER_NOTE]}
    if not args.dry_run:
        try:
            with FileLock(store.state / "locks" / f"worker-{sid}.lock", stale_after=300,
                          timeout=5), \
                    FileLock(store.state / "locks" / "githooks.lock", stale_after=300, timeout=5):
                _remove(store, config, sid, paths, links)
        except LockTimeout:
            return error(f"session {sid} is being written by a worker; try again shortly",
                         args.json)
    if args.json:
        return Reply(json.dumps(result, indent=2))
    verb = "Would remove" if args.dry_run else "Forgot session " + sid + ". Removed"
    lines = [f"{verb}:", *(f"  {p}" for p in paths)]
    lines += [f"  checkpoint {sha[:12]} (no other session)" for sha in dropped]
    lines += [f"  link to {sid} in checkpoint {sha[:12]}" for sha in trimmed]
    return Reply("\n".join([*lines, "", TRAILER_NOTE]))


def _remove(store: StorePaths, config: Config, sid: str, paths: list[str], links: list) -> None:
    """Branch first, so a failure never leaves a local-only removal that a fetch would undo."""
    gone = [store.local.joinpath(*p.split("/")) for p in paths]
    kept = []
    for rec in links:
        if rec.session_ids == [sid]:
            gone.append(store.records / "checkpoints" / f"{rec.sha}.json")
        else:
            kept.append(write_checkpoint(
                store, dataclasses.replace(rec, session_ids=[i for i in rec.session_ids if i != sid]),
                config))
    if read_tip(store):
        remove_paths(store, gone, f"wscontext: forget {sid}")
        if kept:
            commit_paths(store, kept, f"wscontext: forget {sid}")
        # Earlier local commits still hold the files; a later push would send them.
        squash_unshared(store, shared_tips(store), f"wscontext: forget {sid}")
    for path in gone:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    turn_state_path(store, sid).unlink(missing_ok=True)
    seen = store.state / "materialized.json"
    if seen.exists():
        removed = {p.relative_to(store.local).as_posix() for p in gone}

        def clear(d: dict) -> None:
            for key in [k for k in d if k in removed
                        or any(k.startswith(r + "/") for r in removed)]:
                del d[key]

        update_json(seen, clear, config)
