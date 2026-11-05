"""One handler per Claude Code hook event. Each is cheap: heavy work goes to the worker."""

import time
from collections.abc import Callable
from pathlib import Path

from wscontext.capture import worker
from wscontext.capture.kinds import ctx_invocations, tool_kind
from wscontext.capture.state import (active_path, git_author, git_value, idle_sessions,
                                     read_json, turn_state_path, update_json, worktree_key)
from wscontext.config import Config
from wscontext.logs import CTX_CALLS_LOG, append_log, now_iso
from wscontext.store.paths import StorePaths
from wscontext.store.records import RecordError, SessionRecord, read_session, write_session

EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
SWEEP_EVERY_SECONDS = 86400


def _spawn(store: StorePaths, action: str, payload: dict) -> None:
    worker.spawn_worker([action, payload["session_id"], str(payload.get("transcript_path", "")),
                         str(store.root)], store.root)


def _update_state(store: StorePaths, config: Config, payload: dict,
                  change: Callable[[dict], None] = lambda d: None) -> None:
    """Change the session's turn state; every write also records where its transcript is."""
    transcript = payload.get("transcript_path")

    def apply(d: dict) -> None:
        if isinstance(transcript, str) and transcript:
            d["transcript"] = transcript
        change(d)

    update_json(turn_state_path(store, payload["session_id"]), apply, config)


def _sweep_due(store: StorePaths, config: Config) -> bool:
    try:
        if time.time() - float((store.state / "last_sweep").read_text()) < SWEEP_EVERY_SECONDS:
            return bool(idle_sessions(store, config))
    except (OSError, ValueError):
        pass
    return True


def session_start(store: StorePaths, config: Config, payload: dict) -> None:
    sid = payload["session_id"]
    try:
        read_session(store, sid)  # resume, clear and compact keep the original record
    except (OSError, RecordError):
        write_session(store, SessionRecord(
            session_id=sid, author=git_author(store),
            branch=git_value(store, "rev-parse", "--abbrev-ref", "HEAD"),
            worktree=str(store.root), head_start=git_value(store, "rev-parse", "HEAD"),
            started_at=now_iso()), config)
    update_json(active_path(store),
                lambda d: d.setdefault(worktree_key(store, config), {}).setdefault(sid, now_iso()),
                config)
    _update_state(store, config, payload)
    if config.sync.remote:
        _spawn(store, "fetch", payload)
    if _sweep_due(store, config):
        worker.spawn_worker(["sweep", "", "", str(store.root)], store.root)


def user_prompt_submit(store: StorePaths, config: Config, payload: dict) -> None:
    sid = payload["session_id"]
    key = worktree_key(store, config)
    sessions = read_json(active_path(store)).get(key)
    if not isinstance(sessions, dict) or sid not in sessions:
        # finalized after going idle, and continued without a new SessionStart
        update_json(active_path(store), lambda d: d.setdefault(key, {}).setdefault(sid, now_iso()),
                    config)

    def start(d: dict) -> None:
        d.setdefault("turns", []).append({"started_at": now_iso(), "ended_at": None})

    _update_state(store, config, payload, start)


def post_tool_use(store: StorePaths, config: Config, payload: dict) -> None:
    name = str(payload.get("tool_name", "unknown"))
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    command = tool_input.get("command") if isinstance(tool_input.get("command"), str) else None
    kind = tool_kind(name, command)
    calls = ctx_invocations(command) if kind == "ctx" and command else []
    edited = None
    if name in EDIT_TOOLS:
        edited = tool_input.get("file_path") or tool_input.get("notebook_path")

    def count(d: dict) -> None:
        tools = d.setdefault("tool_calls", {})
        tools[kind] = tools.get(kind, 0) + 1
        for sub, _ in calls:
            ctx = d.setdefault("ctx_calls", {})
            ctx[sub] = ctx.get(sub, 0) + 1
        if isinstance(edited, str):
            try:
                rel = Path(edited).resolve().relative_to(store.root.resolve()).as_posix()
            except ValueError:
                return  # outside the repository: not recorded
            if rel not in d.setdefault("files", []):
                d["files"].append(rel)

    _update_state(store, config, payload, count)
    response = payload.get("tool_response")
    out = response.get("stdout") if isinstance(response, dict) else response
    for _, argv in calls:
        append_log(store, config, CTX_CALLS_LOG, {
            "ts": now_iso(), "source": "hook", "session_id": payload["session_id"],
            "cwd": payload.get("cwd"), "argv": argv,
            "output_bytes": len(out.encode("utf-8", "replace")) if isinstance(out, str) else None})


def stop(store: StorePaths, config: Config, payload: dict) -> None:
    def close(d: dict) -> None:
        turns = d.get("turns")
        if turns and turns[-1].get("ended_at") is None:
            turns[-1]["ended_at"] = now_iso()

    _update_state(store, config, payload, close)
    _spawn(store, "copy", payload)


def session_end(store: StorePaths, config: Config, payload: dict) -> None:
    sid = payload["session_id"]

    def drop(d: dict) -> None:
        key = worktree_key(store, config)
        sessions = d.get(key, {})
        sessions.pop(sid, None)
        if not sessions:
            d.pop(key, None)

    update_json(active_path(store), drop, config)
    _spawn(store, "final", payload)


def pre_compact(store: StorePaths, config: Config, payload: dict) -> None:
    _update_state(store, config, payload)
    if config.capture.precompact_snapshot:
        _spawn(store, "snapshot", payload)


HANDLERS = {
    "sessionstart": session_start,
    "userpromptsubmit": user_prompt_submit,
    "posttooluse": post_tool_use,
    "stop": stop,
    "sessionend": session_end,
    "precompact": pre_compact,
}
