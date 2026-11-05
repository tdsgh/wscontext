"""Turns per session and over a window: counts, durations, tool calls by kind, tokens."""

from datetime import datetime

from wscontext.cli.data import parse_time
from wscontext.stats.data import in_window, percentile
from wscontext.store.records import SessionRecord, TurnRecord


def _add(total: dict[str, int], more: dict[str, int] | None) -> None:
    for key, n in (more or {}).items():
        total[key] = total.get(key, 0) + n


def duration(turn: TurnRecord) -> float | None:
    """Seconds from turn start to end; None while the turn has no recorded end."""
    start, end = parse_time(turn.started_at), parse_time(turn.ended_at)
    return (end - start).total_seconds() if start and end else None


def session_row(rec: SessionRecord, since: datetime | None = None) -> dict:
    turns = [t for t in rec.turns if in_window(t.started_at, since)]
    times = [d for t in turns if (d := duration(t)) is not None]
    return {"session_id": rec.session_id, "started_at": rec.started_at, "turns": len(turns),
            "seconds": sum(times) if times else None,
            "tool_calls": rec.tool_calls, "tokens": rec.tokens}


def summarize(sessions: list[SessionRecord], since: datetime | None) -> dict:
    rows = [session_row(r, since) for r in sessions]
    times = [d for r in sessions for t in r.turns
             if in_window(t.started_at, since) and (d := duration(t)) is not None]
    tools: dict[str, int] = {}
    tokens: dict[str, int] = {}
    for rec in sessions:
        _add(tools, rec.tool_calls)
        _add(tokens, rec.tokens)
    return {"sessions": len(rows), "turns": sum(r["turns"] for r in rows),
            "timed_turns": len(times), "turn_seconds_p50": percentile(times, 50),
            "turn_seconds_p95": percentile(times, 95), "tool_calls": tools,
            "tokens": tokens or None, "per_session": rows}


def session_detail(rec: SessionRecord) -> dict:
    return {"session_id": rec.session_id, "branch": rec.branch, "author": rec.author,
            "started_at": rec.started_at, "ended_at": rec.ended_at,
            "ended_by": rec.ended_by, "retrieval": rec.retrieval, "brief_tokens": rec.brief_tokens,
            "tool_calls": rec.tool_calls, "ctx_calls": rec.ctx_calls, "tokens": rec.tokens,
            "turns": [{"index": t.index, "started_at": t.started_at or None,
                       "seconds": duration(t), "tool_calls": t.tool_calls, "tokens": t.tokens}
                      for t in rec.turns]}
