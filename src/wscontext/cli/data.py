"""What the read commands share: session lookup, transcript turns and `--since` values."""

import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wscontext.adapters.base import Item
from wscontext.adapters.claude_code import ClaudeCodeAdapter
from wscontext.gitutil import GitError
from wscontext.history.branch import ls_blobs, read_blob, read_tip
from wscontext.store.paths import StorePaths, is_safe_name
from wscontext.store.records import RecordError, SessionRecord, read_session

_DURATION = re.compile(r"(\d+)([smhdw])")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


@dataclass
class TurnView:
    index: int
    started_at: str | None
    prompt: str | None  # None when the transcript was not captured or its format is unknown
    tool_calls: dict[str, int]
    tokens: dict[str, int] | None
    items: list[Item] = field(default_factory=list)  # full content; empty without a transcript


def parse_time(value: str | None) -> datetime | None:
    """An ISO timestamp as an aware UTC datetime; None when absent or unparseable."""
    try:
        parsed = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_since(text: str, now: datetime | None = None) -> datetime:
    """`7d`, `12h`, `30m` (ago) or an ISO date or timestamp, as an aware UTC datetime."""
    m = _DURATION.fullmatch(text.strip())
    if m:
        return (now or datetime.now(timezone.utc)) - timedelta(**{_UNITS[m.group(2)]: int(m.group(1))})
    parsed = parse_time(text.strip())
    if parsed is None:
        raise ValueError(f"not a date or duration: {text!r}")
    return parsed


def repo_path(store: StorePaths, path: str) -> str:
    """A path given relative to the working directory, as repo-relative POSIX."""
    try:
        return (Path.cwd() / path).resolve().relative_to(store.root.resolve()).as_posix()
    except ValueError:
        return path


def session_status(record: SessionRecord) -> str:
    """`running`, `finished` or `finalized-idle`."""
    if record.ended_at is None:
        return "running"
    return "finalized-idle" if record.ended_by == "idle" else "finished"


def list_sessions(store: StorePaths) -> list[SessionRecord]:
    """Every readable session record, newest start first."""
    found = []
    for path in sorted((store.records / "sessions").glob("*.json")):
        try:
            found.append(read_session(store, path.stem))
        except (OSError, RecordError):
            continue
    return sorted(found, key=lambda r: r.started_at or "", reverse=True)


def find_session(store: StorePaths, ref: str) -> SessionRecord | list[str] | None:
    """The record for an exact session ID or a unique prefix; the candidates when ambiguous."""
    if not is_safe_name(ref):
        return None
    records = list_sessions(store)
    for rec in records:
        if rec.session_id == ref:
            return rec
    matches = [r for r in records if r.session_id.startswith(ref)]
    if len(matches) == 1:
        return matches[0]
    return sorted(r.session_id for r in matches) or None


def transcript_turns(store: StorePaths, record: SessionRecord) -> list[TurnView]:
    """The session's turns with prompts when the captured transcript is readable.

    Falls back to the record's own turns, without prompts, for a missing transcript or an
    unknown format.
    """
    sid = record.session_id
    raw = sorted((store.raw / "sessions").glob(f"????-??-??-{sid}.jsonl"))
    parsed = None
    if raw:
        parsed = ClaudeCodeAdapter().parse(raw[0])
    else:  # pruned from raw/: the history branch still holds the copy
        tip = read_tip(store)
        name = re.compile(rf"raw/sessions/\d{{4}}-\d\d-\d\d-{re.escape(sid)}\.jsonl")
        try:
            paths = sorted(p for p in (ls_blobs(store, tip) if tip else ()) if name.fullmatch(p))
        except GitError:
            paths = []
        data = read_blob(store, paths[-1]) if paths else None
        if data is not None:
            with tempfile.TemporaryDirectory() as tmp:
                copy = Path(tmp) / "t.jsonl"
                copy.write_bytes(data)
                parsed = ClaudeCodeAdapter().parse(copy)
    if parsed is not None and parsed.supported and parsed.turns:
        return [TurnView(t.index, t.started_at, t.prompt, t.tool_calls, t.tokens, t.items)
                for t in parsed.turns]
    return [TurnView(t.index, t.started_at or None, None, t.tool_calls, t.tokens)
            for t in record.turns]


class GraphError(Exception):
    """The graph could not be refreshed or opened."""


def open_graph(store: StorePaths, config):
    """Refresh the graph within `[graph] query_budget_ms`, then open it read-only.

    Returns the connection and the `Freshness` of the refresh; raises `GraphError`.
    """
    import sqlite3

    from wscontext.graph import build, db
    try:
        fresh = build.refresh(store, config, config.graph.query_budget_ms)
        return db.connect(store, readonly=True), fresh
    except (GitError, OSError, sqlite3.Error, db.GraphUnavailable) as e:
        raise GraphError(str(e)) from e


def graph_caveats(fresh) -> tuple[list[str], str]:
    """The call-log flags and the note for what may make a graph answer incomplete."""
    flags, note = [], []
    if fresh.busy:
        flags.append("graph-busy")
        note.append("graph-busy: another process is updating the graph; this answer comes from "
                    "the graph as it is.")
    elif not fresh.complete:
        flags.append("graph-incomplete")
        note.append("graph-incomplete: the graph is still being built, so this answer may be "
                    "missing results; rerun in a moment.")
    if fresh.parse_errors:
        flags.append("parse-errors")
        shown = ", ".join(fresh.parse_errors[:3]) + (", ..." if len(fresh.parse_errors) > 3 else "")
        note.append(f"parse-errors: {len(fresh.parse_errors)} file(s) could not be parsed "
                    f"completely ({shown}).")
    if fresh.unavailable:
        flags.append("language-unavailable")
        note.append("language-unavailable: no parser for " + ", ".join(fresh.unavailable)
                    + "; files in those languages are missing.")
    return flags, "\n".join(note)
