"""What the read commands share: session lookup, transcript turns and `--since` values."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from wscontext.adapters.claude_code import ClaudeCodeAdapter
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
    if raw:
        parsed = ClaudeCodeAdapter().parse(raw[0])
        if parsed.supported and parsed.turns:
            return [TurnView(t.index, t.started_at, t.prompt, t.tool_calls, t.tokens)
                    for t in parsed.turns]
    return [TurnView(t.index, t.started_at or None, None, t.tool_calls, t.tokens)
            for t in record.turns]
