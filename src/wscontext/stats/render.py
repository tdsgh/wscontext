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


def _named(items: list[dict]) -> str:
    return ", ".join(f"{i['name']} {size(i['bytes'])}" for i in items) or "none"


def _groups(d: dict[str, int]) -> str:
    ranked = sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{k} {size(n)}" for k, n in ranked) or "empty"


def _snapshots(figures: dict) -> str:
    snap = figures["snapshots"]
    return f"PreCompact snapshots {size(snap['bytes'])} in {snap['files']} files"


def _warning_line(w: dict) -> str:
    what = f"session {w['id']}" if w["scope"] == "session" else w["scope"]
    line = f"{what}: {size(w['bytes'])} is over the {size(w['limit'])} limit; largest: {_named(w['largest'])}"
    if w.get("largest_kinds"):
        line += f"; by kind: {_named(w['largest_kinds'])}"
    if w.get("largest_sessions"):
        line += f"; by session: {_named(w['largest_sessions'])}"
    return line


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
    t, r, ix = d["transcript"], d["retrieval"], d["index"]
    ratio = (f"; {share(t['stored_of_known_source'] / t['source'])} of the source Claude Code wrote "
             f"({size(t['source'])}, {t['sessions_with_source']} sessions with a source)"
             if t["source"] else "; source transcripts not available for comparison")
    volume = [f"stored transcripts: {size(t['stored'])}{ratio}",
              f"by kind: {_groups(t['kinds'])}",
              f"local store: {size(d['local']['bytes'])} ({_groups(d['local']['groups'])}); "
              f"{_snapshots(d['local'])}; {d['pruned_paths']} raw files pruned",
              f"history branch: {size(d['history_branch']['bytes'])} ({_groups(d['history_branch']['groups'])}); "
              f"{_snapshots(d['history_branch'])}",
              f"shared history: {size(d['shared_history']['bytes'])} ({_groups(d['shared_history']['groups'])}); "
              f"{_snapshots(d['shared_history'])}",
              f"limits: {size(d['limits']['session_warn_bytes'])} per session, "
              f"{size(d['limits']['store_warn_bytes'])} per store"]
    if t["sessions_measured"]:
        volume.append(f"{t['sessions_measured']} sessions captured before schema 2 were measured on their stored copy")
    volume += [f"WARNING {_warning_line(w)}" for w in d["warnings"]] or ["size warnings: none"]
    per_session = table(["session", "stored", "snapshots", "source", "largest kind"],
                        [[v["session_id"], size(v["stored"]), size(v["snapshot_bytes"]), size(v["source"]),
                          _named(_top_kinds(v["kinds"]))] for v in d["per_session"]]) \
        if d["per_session"] else ["no sessions"]
    growth = [[g["week"], size(g["bytes"]), ("+" if g["growth"] >= 0 else "-") + size(abs(g["growth"]))]
              for g in d["growth"]["history_branch"]]
    new_sessions = [[g["week"], g["sessions"], size(g["stored_bytes"])] for g in d["growth"]["sessions"]]
    growth_body = [*(table(["week", "history branch", "growth"], growth) if growth else ["no history commits"]),
                   *(table(["week", "sessions started", "stored"], new_sessions) if new_sessions else [])]
    index = ([f"size: {size(ix['bytes'])}; sessions: {ix['sessions']}; commits: {ix['commits']}; "
              f"documents: {ix['documents']}; lag: {ix['lag']} sources behind captured history"]
             if ix["present"] else ["no index on this machine yet"])
    brief = [f"retrieval switch: {'on' if r['enabled'] else 'off'}; brief: "
             f"{'on' if r['enabled'] and r['brief'] else 'off'}",
             f"sessions with a brief: {r['sessions_with_brief']} of {r['sessions_with_switch']} "
             f"({share(r['brief_share'])}); brief tokens given: {r['brief_tokens']}; "
             f"sessions captured with retrieval on: {r['sessions_retrieval_on']}"]
    summary = {k: v for k, v in d.items() if k not in ("per_session", "growth")}
    return [_block("data", "collected data", body, summary),
            _block("data-volume", "stored volume", volume, {k: d[k] for k in (
                "transcript", "local", "history_branch", "shared_history", "pruned_paths", "limits",
                "warnings")}),
            _block("data-sessions", "largest stored sessions", per_session,
                   {"per_session": d["per_session"]}),
            _block("data-growth", "growth per week", growth_body, {"growth": d["growth"]}),
            _block("data-index", "index", index, {"index": ix}),
            _block("data-brief", "brief and retrieval", brief, {"retrieval": r})]


def _top_kinds(kinds: dict[str, int]) -> list[dict]:
    ranked = sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    return [{"name": k, "bytes": n} for k, n in ranked if n]


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
    a = q["adoption"]
    body += [f"sessions that called ctx: {a['with_ctx']} of {a['sessions']} ({share(a['with_ctx_share'])}); "
             f"called ctx find: {a['with_find']} ({share(a['with_find_share'])})"]
    weekly = [[w["week"], w["sessions"], w["with_ctx"], w["with_find"]] for w in a["by_week"]]
    if weekly:
        body += table(["week", "sessions", "ctx", "find"], weekly)
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


def session_blocks(d: dict, calls: list[dict], first: str | None, volume: dict) -> list[Block]:
    body = [f"{d['branch'] or 'unknown branch'} by {d['author']}; "
            f"started {d['started_at'] or '-'}; ended {d['ended_at'] or 'not recorded'}",
            f"turns: {len(d['turns'])}; tool calls: {counts(d['tool_calls'])}",
            f"ctx calls: {counts(d['ctx_calls'])}",
            "tokens: " + (counts(d["tokens"]) if d["tokens"] else "not exposed by the transcript"),
            f"first search, read or ctx call: {first or 'none or transcript unavailable'}",
            f"ended by: {d['ended_by'] or 'not recorded'}; retrieval: "
            f"{'not recorded' if d['retrieval'] is None else 'on' if d['retrieval'] else 'off'}; "
            f"brief: {'not recorded' if d['brief_tokens'] is None else 'none given' if not d['brief_tokens'] else str(d['brief_tokens']) + ' tokens'}",
            f"stored: {size(volume['stored'])} (source {size(volume['source'])}"
            f"{'; measured on the stored copy' if volume['measured'] else ''}); "
            f"by kind: {_groups(volume['kinds'])}; PreCompact snapshots: {size(volume['snapshot_bytes'])}"]
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
    return [_block(d["session_id"], "session statistics", body, {**summary, "first_tool": first, "volume": volume}),
            turns, queries]
