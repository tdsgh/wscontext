"""`ctx` calls: the CLI-side log joined to sessions through the hook-side log."""

from datetime import datetime

from wscontext.capture.kinds import tool_kind
from wscontext.cli.data import parse_time, transcript_turns
from wscontext.logs import CTX_CALLS_LOG
from wscontext.stats.data import week_of, percentile, read_log
from wscontext.store.paths import StorePaths
from wscontext.store.records import SessionRecord

# The hook logs after the tool call returns; the CLI logs when the call ends. They land this close.
JOIN_WINDOW_SECONDS = 60


def _key(argv: list[str]) -> str:
    """argv with shell quoting dropped: the hook side only sees the command text, split on spaces."""
    return " ".join(" ".join(argv).replace('"', "").replace("'", "").split())


def _seconds(row: dict) -> float:
    stamp = parse_time(row.get("ts"))
    return stamp.timestamp() if stamp else 0.0


def join_calls(store: StorePaths, since: datetime | None) -> list[dict]:
    """Every logged call, oldest first; a CLI line gets its session ID from the matching hook line.

    A hook line pairs with the unused CLI line of the same argv nearest in time, within
    JOIN_WINDOW_SECONDS. Calls made by a person in a terminal have no hook line and no session.
    """
    rows = read_log(store, CTX_CALLS_LOG, since)
    cli = [r for r in rows if r.get("source") == "cli"]
    hook = [r for r in rows if r.get("source") == "hook"]
    taken: set[int] = set()
    joined: list[dict] = []
    for h in sorted(hook, key=_seconds):
        near = [(abs(_seconds(c) - _seconds(h)), i) for i, c in enumerate(cli)
                if i not in taken and _key(c.get("argv", [])) == _key(h.get("argv", []))
                and abs(_seconds(c) - _seconds(h)) <= JOIN_WINDOW_SECONDS]
        if near:
            i = min(near)[1]
            taken.add(i)
            joined.append({**cli[i], "session_id": h.get("session_id")})
        else:
            joined.append({**h, "unmatched": "hook"})
    joined += [c for i, c in enumerate(cli) if i not in taken]
    calls = []
    for row in sorted(joined, key=_seconds):
        argv = [str(a) for a in row.get("argv", [])]
        if not argv or argv[0] == "stats":  # reading the report is not a query
            continue
        calls.append({"ts": row.get("ts"), "session_id": row.get("session_id"),
                      "command": argv[0], "query": " ".join(a for a in argv[1:] if not a.startswith("-")),
                      "exit": row.get("exit"), "ms": row.get("ms"),
                      "output_bytes": row.get("output_bytes"), "results": row.get("results"),
                      "flags": row.get("flags") or []})
    return calls


def first_tool(store: StorePaths, rec: SessionRecord) -> str | None:
    """The kind of the session's first search, read or `ctx` call; None without a readable transcript."""
    for turn in transcript_turns(store, rec):
        for item in turn.items:
            if item.kind != "tool_call" or item.name is None:
                continue
            command = item.text.split("command=", 1)[1] if item.name == "Bash" \
                and "command=" in item.text else None
            kind = tool_kind(item.name, command)
            if kind in {"ctx", "search", "read"}:
                return kind
    return None


def ctx_first(store: StorePaths, sessions: list[SessionRecord]) -> dict:
    firsts = {r.session_id: first_tool(store, r) for r in sessions}
    considered = [k for k in firsts.values() if k is not None]
    first = sum(k == "ctx" for k in considered)
    return {"sessions_considered": len(considered), "ctx_first": first,
            "ctx_first_share": first / len(considered) if considered else None,
            "first_tool": firsts}


def adoption(sessions: list[SessionRecord]) -> dict:
    """Sessions that called `ctx` at all and `ctx find`, overall and by week of the start."""
    def row(group: list[SessionRecord]) -> dict:
        return {"sessions": len(group), "with_ctx": sum(bool(sum(r.ctx_calls.values())) for r in group),
                "with_find": sum(bool(r.ctx_calls.get("find")) for r in group)}

    weeks: dict[str, list[SessionRecord]] = {}
    for rec in sessions:
        if week := week_of(rec.started_at):
            weeks.setdefault(week, []).append(rec)
    total = row(sessions)
    return {**total, "with_ctx_share": total["with_ctx"] / len(sessions) if sessions else None,
            "with_find_share": total["with_find"] / len(sessions) if sessions else None,
            "by_week": [{"week": w, **row(weeks[w])} for w in sorted(weeks)]}


def summarize(calls: list[dict], first: dict, adopted: dict) -> dict:
    by_command: dict[str, int] = {}
    flags: dict[str, int] = {}
    for c in calls:
        by_command[c["command"]] = by_command.get(c["command"], 0) + 1
        for f in c["flags"]:
            flags[f] = flags.get(f, 0) + 1
    times = [c["ms"] for c in calls if isinstance(c["ms"], (int, float))]
    sizes = [c["output_bytes"] for c in calls if isinstance(c["output_bytes"], int)]
    counts = [c["results"] for c in calls if isinstance(c["results"], int)]
    return {"calls": len(calls), "from_sessions": sum(c["session_id"] is not None for c in calls),
            "failed": sum(bool(c["exit"]) for c in calls), "by_command": by_command,
            "ms_p50": percentile(times, 50), "ms_p95": percentile(times, 95),
            "output_bytes_total": sum(sizes), "results_total": sum(counts), "flags": flags,
            "sessions_considered": first["sessions_considered"],
            "ctx_first": first["ctx_first"], "ctx_first_share": first["ctx_first_share"],
            "adoption": adopted}
