"""`ctx why <file>[:<line>[-<end>]]` or `<file>#<symbol>`: the commits, sessions and prompts behind code."""

import re
from contextlib import closing
from pathlib import Path

from wscontext.cli.data import (GraphError, find_session, graph_caveats, open_graph, parse_time,
                                transcript_turns)
from wscontext.cli.output import Block, Reply, error, quote, render
from wscontext.config import Config
from wscontext.graph import query
from wscontext.history import trace
from wscontext.store.paths import StorePaths
from wscontext.store.records import RecordError, read_checkpoint

DEFAULT_COMMITS = 5
PROMPTS_COMPACT = 3

_LINES = re.compile(r"(.+):(\d+)(?:-(\d+))?")
_DEF = (r"^[ \t]*(?:(?:export|public|private|protected|static|async|pub|final|abstract|default)\s+)*"
        r"(?:(?:def|class|function|func|fn|interface|struct|enum|trait|type|const|let|var|impl)\s+\*?\s*{name}\b"
        r"|{name}\s*[:=]\s*(?:async\s*)?(?:\(|function\b))")
_CLOSER = ("}", ")", "]", "end")


def add_arguments(parser) -> None:
    parser.add_argument("target", help="FILE, FILE:LINE, FILE:START-END, FILE#SYMBOL or #NAME")


def parse_target(text: str) -> tuple[str, int | None, int | None, str | None]:
    path, hash_, symbol = text.rpartition("#")
    if hash_ and symbol:
        return path, None, None, symbol
    m = _LINES.fullmatch(text)
    if m:
        start = int(m.group(2))
        return m.group(1), start, int(m.group(3) or start), None
    return text, None, None, None


def resolve_symbol(source: str, symbol: str) -> tuple[int, int] | None:
    """The 1-based line range of the first definition of `symbol`, found by regex.

    The range runs to the line before the next non-blank line indented no deeper than the
    definition; a closing bracket at that indent belongs to the definition.
    """
    pattern = re.compile(_DEF.format(name=re.escape(symbol)))
    rows = source.splitlines()
    for i, row in enumerate(rows):
        if pattern.match(row):
            indent = len(row) - len(row.lstrip())
            end = i
            for j in range(i + 1, len(rows)):
                nxt = rows[j]
                if not nxt.strip():
                    continue
                if len(nxt) - len(nxt.lstrip()) <= indent:
                    if nxt.strip().startswith(_CLOSER):
                        end = j
                    break
                end = j
            return i + 1, end + 1
    return None


def _ranges(lines: list[int]) -> str:
    out, lo, prev = [], lines[0], lines[0]
    for n in [*sorted(lines)[1:], None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        out.append(str(lo) if lo == prev else f"{lo}-{prev}")
        if n is not None:
            lo = prev = n
    return ",".join(out)


def _sessions(store: StorePaths, info: trace.CommitInfo, full: bool) -> list[dict]:
    """The sessions behind a commit: trailers plus any checkpoint, with the prompts before it."""
    ids = list(info.sessions)
    try:
        ids += [s for s in read_checkpoint(store, info.sha).session_ids if s not in ids]
    except (OSError, RecordError):
        pass
    committed = parse_time(info.date)
    found = []
    for sid in ids:
        record = find_session(store, sid)
        if record is None or isinstance(record, list) or record.session_id != sid:
            found.append({"id": sid, "available": False})
            continue
        turns = transcript_turns(store, record)
        # Turns are not tied to files in the capture, so: the prompts up to the commit.
        before = [t for t in turns
                  if t.prompt is not None and (committed is None or parse_time(t.started_at) is None
                                               or parse_time(t.started_at) <= committed)]
        shown = before if full else before[-PROMPTS_COMPACT:]
        found.append({"id": sid, "available": True, "date": (record.started_at or "")[:10] or None,
                      "branch": record.branch, "author": record.author,
                      "prompts": [{"turn": t.index, "text": t.prompt} for t in shown],
                      "prompts_omitted": len(before) - len(shown),
                      "prompts_available": any(t.prompt is not None for t in turns)})
    return found


def _commit_block(store: StorePaths, info: trace.CommitInfo, lines: list[int],
                  full: bool) -> Block:
    sessions = _sessions(store, info, full)
    body = [f"lines {_ranges(lines)} ({len(lines)}) by {info.author}: {info.subject}"]
    links = []
    for s in sessions:
        if not s["available"]:
            body.append(f"session {s['id']}: record not available locally (history not synced?)")
            continue
        links.append(f"ctx show {s['id']}")
        meta = ", ".join(x for x in (s["date"], s["branch"]) if x)
        body.append(f"session {s['id']}" + (f" ({meta})" if meta else ""))
        if not s["prompts_available"]:
            body.append("  prompts unavailable: transcript not captured or format not recognised")
        elif s["prompts_omitted"]:
            body.append(f"  ({s['prompts_omitted']} earlier prompts not shown)")
        for prompt in s["prompts"]:
            body += [f"  turn {prompt['turn']}:", *("  " + q for q in quote(prompt["text"]).split("\n"))]
    if not sessions:
        body.append("no linked session: commit made outside a captured session")
    return Block("commit", info.sha[:12], info.subject, info.date[:10], body, links,
                 {"sha": info.sha, "author": info.author, "lines": sorted(lines),
                  "linked": bool(sessions), "sessions": sessions})


def _graph_sessions(symbol: query.Symbol, links: list[query.SessionLink],
                    seen: set[str]) -> list[Block]:
    """Sessions that changed the definition, from the graph, not already shown through blame."""
    by_session: dict[str, list[query.SessionLink]] = {}
    for link in links:
        if link.session_id not in seen:
            by_session.setdefault(link.session_id, []).append(link)
    if not by_session:
        return []
    body = [f"session {sid} ({(rows[0].committed_at or '')[:10] or 'undated'}, commit "
            + ", ".join(r.sha[:12] for r in rows) + ")" for sid, rows in by_session.items()]
    return [Block("sessions", f"{symbol.path}#{symbol.qualname}",
                  f"sessions that changed this {symbol.kind}", None, body,
                  [f"ctx show {sid}" for sid in by_session],
                  {"sessions": [{"id": sid, "shas": [r.sha for r in rows],
                                 "committed_at": rows[0].committed_at}
                                for sid, rows in by_session.items()]})]


def _flags(blocks: list[Block]) -> list[str]:
    """Freshness caveats of an answer, for the call log."""
    flags = set()
    for b in blocks:
        if b.type == "uncommitted":
            flags.add("uncommitted")
        for s in b.extra.get("sessions", []) if b.type == "commit" else []:
            if not s["available"]:
                flags.add("session-unavailable")
            elif not s["prompts_available"]:
                flags.add("prompts-unavailable")
    return sorted(flags)


def _candidates(args, symbols, flags: list[str], caveats: str) -> Reply:
    from wscontext.cli.code import definition_block
    blocks = [definition_block(s) for s in symbols]
    for b in blocks:
        b.links = b.links[1:]
    pick = "Several definitions match; rerun `ctx why` with one of them as FILE#NAME."
    text = render(f"ctx why {args.target}", blocks, full=args.full, as_json=args.json,
                  note="\n".join(x for x in (pick, caveats) if x),
                  meta={"target": args.target, "ambiguous": True, "flags": flags})
    return Reply(text, 1, results=len(blocks), flags=flags, report=True)


def run(args, store: StorePaths, config: Config) -> Reply:
    path, start, end, symbol = parse_target(args.target)
    flags: list[str] = []
    caveats = ""
    graph_symbol = None
    if path:
        absolute = (Path.cwd() / path).resolve()
        try:
            rel = absolute.relative_to(store.root.resolve()).as_posix()
        except ValueError:
            return error(f"{path} is outside the repository", args.json)
        if not absolute.is_file():
            return error(f"{path} is not a file", args.json)
    if symbol is not None:
        try:
            conn, fresh = open_graph(store, config)
        except GraphError:
            flags.append("graph-unavailable")
            if not path:
                return error("no code graph available, so a bare name cannot be resolved; "
                             "run `ctx rebuild --graph` or give FILE#NAME", args.json, flags=flags)
        else:
            more, caveats = graph_caveats(fresh)
            flags += more
            with closing(conn):
                resolved = query.resolve(conn, f"{rel if path else ''}#{symbol}")
                known = not path or query.file_outline(conn, rel) is not None
                if len(resolved.symbols) == 1:
                    graph_symbol = resolved.symbols[0]
                    sessions = query.sessions_for(conn, graph_symbol.path, graph_symbol.qualname)
            if len(resolved.symbols) > 1:
                return _candidates(args, resolved.symbols, flags, caveats)
            if not resolved.symbols and known:
                return error(f"no definition of {symbol!r} found"
                             + (f" in {rel}" if path else ""), args.json, flags=flags)
        if graph_symbol is not None:
            rel = graph_symbol.path
            start, end = graph_symbol.start, graph_symbol.end
        else:
            # the graph cannot answer for this file: unavailable, or a language it does not parse
            found = resolve_symbol(absolute.read_text(encoding="utf-8", errors="replace"), symbol)
            if found is None:
                return error(f"no definition of {symbol!r} found in {rel}", args.json,
                             flags=flags)
            start, end = found
        absolute = store.root / rel
    try:
        blamed = trace.blame(store, rel, start, end)
    except trace.NotTracked:
        last = end if start is not None else len(absolute.read_bytes().splitlines())
        blamed = {trace.UNCOMMITTED: list(range(start or 1, last + 1))}
    except ValueError as e:
        return error(str(e).removeprefix("fatal: "), args.json)
    infos = trace.commits(store, [s for s in blamed if s != trace.UNCOMMITTED])
    ordered = sorted(infos.values(), key=lambda i: i.date, reverse=True)
    blocks = []
    if trace.UNCOMMITTED in blamed:
        lines = blamed[trace.UNCOMMITTED]
        blocks.append(Block("uncommitted", "working-tree", "changes not committed yet", None,
                            [f"lines {_ranges(lines)} ({len(lines)}) have uncommitted changes"],
                            extra={"lines": sorted(lines)}))
    limit = args.limit or DEFAULT_COMMITS
    shown = ordered if args.full and not args.limit else ordered[:limit]
    blocks += [_commit_block(store, i, blamed[i.sha], args.full) for i in shown]
    where = rel + (f"#{symbol}" if symbol else f":{start}-{end}" if start is not None else "")
    if graph_symbol is not None:
        seen = {x["id"] for b in blocks if b.type == "commit"
                for x in b.extra["sessions"]}
        blocks += _graph_sessions(graph_symbol, sessions, seen)
    left = len(ordered) - len(shown)
    return Reply(render(f"ctx why {where}", blocks, full=args.full, as_json=args.json,
                        omitted=left, hint="Rerun with --limit N or --full.",
                        note=caveats,
                        meta={"file": rel, "lines": [start, end] if start is not None else None,
                              "symbol": symbol}),
                results=len(ordered), flags=sorted({*_flags(blocks), *flags}))
