"""`ctx impact <target>`: what a change to a definition or file touches, and who changed it before."""

import json
from contextlib import closing
from datetime import datetime, timezone
from pathlib import PurePosixPath

from wscontext.cli.code import OFF_MESSAGE as CODE_OFF
from wscontext.cli.code import definition_block
from wscontext.cli.data import (GraphError, graph_caveats, list_sessions, open_graph, parse_time,
                                 repo_path)
from wscontext.cli.output import Block, Reply, error, render
from wscontext.config import Config
from wscontext.graph import query
from wscontext.graph.languages import load_parts
from wscontext.store.paths import StorePaths

EPOCH = datetime.min.replace(tzinfo=timezone.utc)
DEFAULT_LIMIT = 5  # per section
OFF_MESSAGE = CODE_OFF.replace("ctx code", "ctx impact")
FOOTER = ("Calls are matched by name only: a caller may call another definition with the same "
          "name, and calls through a variable, a registry or a string are not found.")


def add_arguments(parser) -> None:
    parser.add_argument("target", help="FILE, FILE#NAME, FILE#Class.method, #NAME or NAME")
    parser.add_argument("--depth", type=int, choices=[1, 2, 3], default=1,
                        help="how many levels of callers to follow")


def _target(store: StorePaths, text: str) -> str:
    """The target with its path made repository-relative."""
    path, hash_, name = text.partition("#")
    if hash_:
        return (repo_path(store, path) if path else "") + "#" + name
    return repo_path(store, text) if (store.root / repo_path(store, text)).is_file() else text


def _callers(conn, symbols: list[query.Symbol], path: str, depth: int) -> list[dict]:
    """Callers by short name, breadth first. A file target (no symbols) takes the names of its
    top-level definitions and leaves out calls from inside the file."""
    if symbols:
        start = {s.name: s.id for s in symbols}
    else:
        outline = query.file_outline(conn, path)
        start = {d.name: None for d in outline.definitions if d.parent is None} if outline else {}
    found: dict[tuple, dict] = {}
    seen, names = set(), start
    for level in range(1, depth + 1):
        nxt: dict[str, int | None] = {}
        for name, own in sorted(names.items()):
            shared = query.name_count(conn, name)
            for call in query.callers_of(conn, name):
                caller, cpath, line = call.caller, call.path, call.line
                if (caller is not None and caller.id == own) or (not symbols and level == 1
                                                                 and cpath == path):
                    continue
                qualname = caller.qualname if caller else None
                key = (cpath, qualname)
                if key in found:
                    found[key]["lines"].append(line)
                    continue
                if key in seen:
                    continue
                seen.add(key)
                found[key] = {"path": cpath, "qualname": qualname,
                              "start": caller.start if caller else None,
                              "end": caller.end if caller else None,
                              "lines": [line], "calls": name, "shared": shared, "depth": level}
                if caller is not None:
                    nxt[caller.name] = caller.id
        names = {n: i for n, i in nxt.items() if n not in start}
    return list(found.values())


def _caller_block(c: dict, test: bool) -> Block:
    where = f"{c['path']}:{c['start']}-{c['end']}" if c["qualname"] else f"{c['path']}:{c['lines'][0]}"
    title = c["qualname"] or "module level"
    body = [f"name match: calls `{c['calls']}` ({c['shared']} definition"
            f"{'s' * (c['shared'] != 1)} share this name)",
            "line " + ", ".join(map(str, c["lines"][:5]))]
    if c["depth"] > 1:
        body.append(f"depth {c['depth']}: a caller of a caller")
    links = [f"ctx why {c['path']}#{c['qualname']}"] if c["qualname"] else [f"ctx why {c['path']}"]
    return Block("test" if test else "caller", where, title, None, body, links,
                 {**c, "match": "name", "kind": "caller"})


def run(args, store: StorePaths, config: Config) -> Reply:
    if not config.retrieval.enabled:
        if args.json:
            return Reply(json.dumps({"command": "ctx impact", "enabled": False,
                                     "message": OFF_MESSAGE}, indent=2), results=0)
        return Reply(OFF_MESSAGE, results=0)
    try:
        conn, fresh = open_graph(store, config)
    except GraphError as e:
        return error(f"no code graph available ({e}); run `ctx rebuild --graph`", args.json)
    flags, caveats = graph_caveats(fresh)
    command = f"ctx impact {args.target}"
    with closing(conn):
        resolved = query.resolve(conn, _target(store, args.target))
        if resolved.path is not None and not resolved.symbols and query.file_outline(
                conn, resolved.path) is None:
            return error(f"{resolved.path} is not in the graph: Git ignores it or its language "
                         "is not supported", args.json, flags=flags)
        if not resolved.symbols and ("#" in args.target or resolved.path is None):
            return error(f"no definition matches {args.target!r}", args.json, flags=flags)
        if len(resolved.symbols) > 1:
            blocks = [definition_block(s) for s in resolved.symbols]
            pick = "Several definitions match; rerun `ctx impact` with one of them."
            text = render(command, blocks, full=args.full, as_json=args.json,
                          note="\n".join(x for x in (pick, caveats) if x),
                          meta={"target": args.target, "ambiguous": True, "flags": flags})
            return Reply(text, 1, results=len(blocks), flags=flags, report=True)
        symbol = resolved.symbols[0] if resolved.symbols else None
        path = symbol.path if symbol else resolved.path
        qualname = symbol.qualname if symbol else None
        outline = query.file_outline(conn, path)
        by_ext = {e: p for p in load_parts(config.graph.languages)[0] for e in p.extensions}

        def is_test(p: str) -> bool:
            part = by_ext.get(PurePosixPath(p).suffix.lower())
            return part is not None and part.is_test(p)

        callers = _callers(conn, [symbol] if symbol else [], path, args.depth)
        importers = list({e.path: e for e in outline.importers}.values()) if outline else []
        cochange = query.cochanged(conn, path)
        links = query.sessions_for(conn, path, qualname)

    limit = args.limit or (None if args.full else DEFAULT_LIMIT)
    sections: dict[str, list[Block]] = {"callers": [], "tests": [], "importers": [],
                                        "co-changed files": [], "sessions": []}
    for c in callers:
        test = is_test(c["path"])
        sections["tests" if test else "callers"].append(_caller_block(c, test))
    for e in importers:
        test = is_test(e.path)
        sections["tests" if test else "importers"].append(Block(
            "test" if test else "importer", e.path, f"imports {path}", None,
            [f"line {e.line}: {e.spec}"], [f"ctx code {e.path}"],
            {"path": e.path, "line": e.line, "spec": e.spec, "kind": "importer"}))
    for c in cochange:
        sections["co-changed files"].append(Block(
            "cochange", c.path, f"changed together with {path}", None,
            [f"{c.count} commits, {c.share:.0%} of the commits that changed {path}"],
            [f"ctx code {c.path}"], {"path": c.path, "count": c.count, "share": c.share}))
    by_session: dict[str, list[query.SessionLink]] = {}
    for link in links:
        by_session.setdefault(link.session_id, []).append(link)
    what = f"{path}#{qualname}" if qualname else path
    dated: list[tuple[datetime, Block]] = []
    for sid, rows in by_session.items():
        dated.append((parse_time(rows[0].committed_at) or EPOCH, Block(
            "session", sid, f"changed {what}", (rows[0].committed_at or "")[:10] or None,
            ["in commit " + ", ".join(r.sha[:12] for r in rows)], [f"ctx show {sid}"],
            {"session_id": sid, "shas": [r.sha for r in rows],
             "committed_at": rows[0].committed_at})))
    if not qualname:
        for rec in list_sessions(store):
            if path in rec.files_touched and rec.session_id not in by_session:
                dated.append((parse_time(rec.started_at) or EPOCH, Block(
                    "session", rec.session_id, f"edited {path}", (rec.started_at or "")[:10] or None,
                    ["no linked commit"], [f"ctx show {rec.session_id}"],
                    {"session_id": rec.session_id, "shas": [], "committed_at": None})))
    sections["sessions"] = [b for _, b in sorted(dated, key=lambda d: d[0], reverse=True)]
    omitted, blocks = 0, []
    summary = {k: len(v) for k, v in sections.items()}
    head = (definition_block(symbol) if symbol else Block(
        "file", path, outline.language if outline else "", None,
        [f"{len(outline.definitions)} definitions"] if outline else [], [f"ctx code {path}"]))
    head.type = "target"
    blocks.append(head)
    for blist in sections.values():
        shown = blist if limit is None else blist[:limit]
        omitted += len(blist) - len(shown)
        blocks += shown
    counts = ", ".join(f"{n} {k}" for k, n in summary.items())
    head.body.append(f"found: {counts}")
    note = "\n".join(x for x in (FOOTER, caveats) if x)
    return Reply(render(command, blocks, full=args.full, as_json=args.json, omitted=omitted,
                        hint="Rerun with --limit N or --full.", note=note,
                        meta={"target": args.target, "path": path, "qualname": qualname,
                              "depth": args.depth, "counts": summary, "flags": flags}),
                 results=len(blocks) - 1, flags=flags)
