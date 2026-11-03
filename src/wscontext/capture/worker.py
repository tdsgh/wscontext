"""The detached worker: transcript copy, redaction, parsing, record writing and history commits.

Run as `python -m wscontext.capture.worker <action> <session-id> <transcript> <cwd>`.
Actions: `fetch`, `copy`, `snapshot`, `final`.
"""

import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from wscontext.adapters.claude_code import ClaudeCodeAdapter
from wscontext.capture.kinds import tool_kind
from wscontext.capture.state import (git_author, git_value, read_json, turn_state_path)
from wscontext.config import Config, load_config
from wscontext.history import sync
from wscontext.history.branch import commit_paths
from wscontext.logs import HOOKS_LOG, append_log, now_iso
from wscontext.redact.redactor import redact_bytes
from wscontext.store.locks import FileLock
from wscontext.store.paths import StorePaths, is_safe_name
from wscontext.store.records import (RecordError, SessionRecord, TurnRecord, atomic_write,
                                     read_session, write_session)

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def detach_kwargs(platform: str) -> dict:
    if platform == "win32":
        return {"creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def spawn_worker(args: list[str], cwd: Path, module: str = "wscontext.capture.worker") -> None:
    subprocess.Popen([sys.executable, "-m", module, *args],
                     cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, **detach_kwargs(sys.platform))


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


def _copy_redacted(source: Path, target: Path, config: Config) -> int:
    data, hits = redact_bytes(source.read_bytes(), config)
    atomic_write(target, data)
    return len(hits)


def _read_record(store: StorePaths, session_id: str) -> SessionRecord | None:
    try:
        return read_session(store, session_id)
    except (OSError, RecordError):
        return None


def _final(store: StorePaths, config: Config, session_id: str, transcript: Path,
           raw: Path) -> None:
    copied: list[Path] = []
    record = _read_record(store, session_id) or SessionRecord(
        session_id=session_id, author=git_author(store), worktree=str(store.root),
        started_at=now_iso())
    state = read_json(turn_state_path(store, session_id))
    parsed = ClaudeCodeAdapter().parse(transcript)
    turns: list[TurnRecord] = []
    if parsed.supported:
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
                _copy_redacted(ref.path, target, config)
                copied.append(target)
        record.subagents = [r.agent_id for r in parsed.subagents if is_safe_name(r.agent_id)]
    else:
        # unknown format: only the hook-side turn times survive
        for i, t in enumerate(state.get("turns", [])):
            turns.append(TurnRecord(session_id=session_id, author=record.author, index=i,
                                    started_at=t.get("started_at") or "",
                                    ended_at=t.get("ended_at")))
    token_totals: dict[str, int] = {}
    for t in turns:
        for key, value in (t.tokens or {}).items():
            token_totals[key] = token_totals.get(key, 0) + value
    record.turns = turns
    record.tokens = token_totals or None
    record.tool_calls = state.get("tool_calls", {})
    record.ctx_calls = state.get("ctx_calls", {})
    record.files_touched = sorted(set(record.files_touched) | set(state.get("files", [])))
    record.ended_at = now_iso()
    record.head_end = git_value(store, "rev-parse", "HEAD")
    write_session(store, record, config)
    commit_paths(store, [raw, *copied, store.records / "sessions" / f"{session_id}.json"],
                 f"wscontext: session {session_id}")
    turn_state_path(store, session_id).unlink(missing_ok=True)


def run(action: str, session_id: str, transcript: str, cwd: str) -> None:
    store = StorePaths.resolve(Path(cwd))
    config = load_config(store.root)
    started = time.monotonic()
    entry = {"ts": now_iso(), "event": f"worker:{action}", "session_id": session_id, "ok": True}
    try:
        if action == "fetch":
            entry["changed"] = sync.fetch(store, config)
        elif action in {"copy", "snapshot", "final"}:
            if not is_safe_name(session_id):
                raise ValueError("unsafe session id")
            source = Path(transcript)
            # Stop and SessionEnd workers can overlap; serialize them per session.
            with FileLock(store.state / "locks" / f"worker-{session_id}.lock",
                          stale_after=300, timeout=120):
                raw = _raw_name(store, session_id, _read_record(store, session_id),
                                action == "snapshot")
                entry["redactions"] = _copy_redacted(source, raw, config)
                if action == "final":
                    _final(store, config, session_id, source, raw)
        else:
            raise ValueError(f"unknown worker action {action!r}")
    except Exception as e:
        entry["ok"] = False
        entry["error"] = f"{type(e).__name__}: {e}"
    entry["ms"] = round((time.monotonic() - started) * 1000)
    try:
        append_log(store, config, HOOKS_LOG, entry)
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
