"""Terminal tables for the stats views. Each section is one block; `--json` carries the same numbers."""

from wscontext.cli.output import Block


def table(headers: list[str], rows: list[list]) -> list[str]:
    cells = [headers, *([("-" if c is None else str(c)) for c in row] for row in rows)]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    return [("  ".join(c.ljust(w) for c, w in zip(r, widths))).rstrip() for r in cells]


def share(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def size(n: int | None) -> str:
    if n is None:
        return "-"
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}s" if value >= 10 else f"{value:.1f}s"


def ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f} ms"


def counts(d: dict[str, int]) -> str:
    return ", ".join(f"{k} {n}" for k, n in sorted(d.items())) or "none"


def _block(view: str, title: str, body: list[str], data: dict) -> Block:
    return Block("stats", view, title, None, body, [], data)


def data_blocks(d: dict) -> list[Block]:
    s = d["sync"]
    if s["sharing"]:
        behind = "unknown" if s["behind"] is None else f"{s['ahead']} ahead, {s['behind']} behind"
        sharing = f"on, remote {s['remote']}: {behind}; {s['pending_sessions']} sessions pending"
    else:
        sharing = "off (no sync remote set)"
    body = [f"sessions: {d['sessions']}; turns: {d['turns']}; transcript events: {d['transcript_events']}",
            f"commits: {d['commits']}; linked to a session: {d['linked']} ({share(d['linked_share'])})",
            f"store: {size(d['store_bytes'])}; history branch: {size(d['branch_bytes'])}",
            f"redaction hits: {d['redaction_hits']} across {d['transcript_copies']} transcript copies",
            f"sharing: {sharing}"]
    return [_block("data", "collected data", body, d)]


def turns_blocks(t: dict) -> list[Block]:
    body = [f"sessions: {t['sessions']}; turns: {t['turns']}",
            f"turn duration: p50 {seconds(t['turn_seconds_p50'])}, p95 {seconds(t['turn_seconds_p95'])}"
            f" ({t['timed_turns']} timed)",
            f"tool calls: {counts(t['tool_calls'])}",
            "tokens: " + (counts(t["tokens"]) if t["tokens"] else "not exposed by the transcript")]
    rows = [[r["session_id"], (r["started_at"] or "")[:10], r["turns"], seconds(r["seconds"]),
             counts(r["tool_calls"])] for r in t["per_session"]]
    sessions = _block("turns-sessions", "per session",
                      table(["session", "date", "turns", "time", "tools"], rows) if rows else
                      ["no sessions"], {"per_session": t["per_session"]})
    summary = {k: v for k, v in t.items() if k != "per_session"}
    return [_block("turns", "turns over the window", body, summary), sessions]


def queries_blocks(q: dict, calls: list[dict], shown: int) -> list[Block]:
    body = [f"calls: {q['calls']} ({q['from_sessions']} from sessions, {q['failed']} failed); "
            f"by command: {counts(q['by_command'])}",
            f"latency: p50 {ms(q['ms_p50'])}, p95 {ms(q['ms_p95'])}; "
            f"answers: {size(q['output_bytes_total'])}; results returned: {q['results_total']}",
            f"freshness flags: {counts(q['flags'])}",
            f"ctx before first search or read: {q['ctx_first']} of {q['sessions_considered']} "
            f"sessions ({share(q['ctx_first_share'])})"]
    rows = [[(c["ts"] or "")[:19], c["session_id"] or "-", c["command"], c["query"][:40],
             c["results"], size(c["output_bytes"]), ms(c["ms"]), ",".join(c["flags"]) or "-"]
            for c in calls[-shown:]] if shown else []
    listing = _block("queries-calls", f"last {len(rows)} of {len(calls)} calls",
                     table(["time", "session", "cmd", "query", "results", "size", "latency",
                            "flags"], rows) if rows else ["no calls"], {"calls": calls})
    return [_block("queries", "ctx calls", body, q), listing]


def hooks_blocks(h: dict) -> list[Block]:
    rows = [[e["event"], e["runs"], e["failures"], ms(e["p50_ms"]), ms(e["p95_ms"])]
            for e in h["events"]]
    body = [f"runs: {h['runs']}; failures: {h['failures']}",
            *(table(["event", "runs", "failed", "p50", "p95"], rows) if rows else ["no runs logged"])]
    blocks = [_block("hooks", "capture health", body, {k: v for k, v in h.items()
                                                      if k != "recent_failures"})]
    if h["recent_failures"]:
        lines = [f"{f['ts']} {f['event']} {f['session_id'] or '-'}: {f['error']}"
                 for f in h["recent_failures"]]
        blocks.append(_block("hooks-failures", "recent failures", lines,
                             {"recent_failures": h["recent_failures"]}))
    return blocks


def session_blocks(d: dict, calls: list[dict], first: str | None) -> list[Block]:
    body = [f"{d['branch'] or 'unknown branch'} by {d['author']}; "
            f"started {d['started_at'] or '-'}; ended {d['ended_at'] or 'not recorded'}",
            f"turns: {len(d['turns'])}; tool calls: {counts(d['tool_calls'])}",
            f"ctx calls: {counts(d['ctx_calls'])}",
            "tokens: " + (counts(d["tokens"]) if d["tokens"] else "not exposed by the transcript"),
            f"first search, read or ctx call: {first or 'none or transcript unavailable'}"]
    rows = [[t["index"], (t["started_at"] or "")[:19], seconds(t["seconds"]),
             counts(t["tool_calls"])] for t in d["turns"]]
    turns = _block("session-turns", "turns",
                   table(["turn", "started", "time", "tools"], rows) if rows else ["no turns"],
                   {"turns": d["turns"]})
    call_rows = [[(c["ts"] or "")[:19], c["command"], c["query"][:40], c["results"],
                  size(c["output_bytes"]), ms(c["ms"]), ",".join(c["flags"]) or "-"] for c in calls]
    queries = _block("session-calls", "ctx calls",
                     table(["time", "cmd", "query", "results", "size", "latency", "flags"],
                           call_rows) if call_rows else ["no ctx calls joined to this session"],
                     {"calls": calls})
    summary = {k: v for k, v in d.items() if k != "turns"}
    return [_block(d["session_id"], "session statistics", body, {**summary, "first_tool": first}),
            turns, queries]
