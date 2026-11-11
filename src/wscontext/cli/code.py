"""`ctx code <name|path>`: where code is defined, or what a file holds."""

import json
from contextlib import closing
from pathlib import Path

from wscontext.cli.data import GraphError, graph_caveats, open_graph, repo_path
from wscontext.cli.output import Block, Reply, error, render
from wscontext.config import Config
from wscontext.graph import query
from wscontext.store.paths import StorePaths

DEFAULT_LIMIT = 10
COUNT_UP_TO = 100  # matches counted so the answer can say how many were left out
OFF_MESSAGE = ("ctx code: search is switched off for this repository "
               "([retrieval] enabled = false in .wscontext/config.toml).")


def add_arguments(parser) -> None:
    parser.add_argument("target", help="a definition name, or a file path for its outline")
    parser.add_argument("--kind", choices=["function", "class", "method", "type"],
                        help="only definitions of this kind")


def definition_block(s: query.Symbol) -> Block:
    where = f"{s.path}:{s.start}-{s.end}"
    body = [s.signature] + ([f"in {s.parent}"] if s.parent else [])
    return Block("definition", where, f"{s.kind} {s.qualname}", None, body,
                 [f"ctx impact {s.path}#{s.qualname}", f"ctx why {s.path}#{s.qualname}"],
                 {"path": s.path, "start": s.start, "end": s.end, "kind": s.kind,
                  "name": s.name, "qualname": s.qualname, "signature": s.signature,
                  "parent": s.parent})


def _file_block(o: query.Outline) -> Block:
    body = [f"{o.language}, {len(o.definitions)} definitions"]
    if o.status != "ok":
        body.append(f"parse status {o.status}" + (f": {o.error}" if o.error else ""))
    body += [f"imports {e.target}" for e in o.imports if e.target]
    body += [f"imported by {e.path}" for e in o.importers]
    return Block("file", o.path, o.language, None, body, [f"ctx impact {o.path}"],
                 {"language": o.language, "status": o.status, "error": o.error,
                  "imports": [{"spec": e.spec, "target": e.target, "line": e.line}
                              for e in o.imports],
                  "imported_by": [{"path": e.path, "line": e.line} for e in o.importers]})


def run(args, store: StorePaths, config: Config) -> Reply:
    if not config.retrieval.enabled:
        if args.json:
            return Reply(json.dumps({"command": "ctx code", "enabled": False,
                                     "message": OFF_MESSAGE}, indent=2), results=0)
        return Reply(OFF_MESSAGE, results=0)
    try:
        conn, fresh = open_graph(store, config)
    except GraphError as e:
        return error(f"no code graph available ({e}); run `ctx rebuild --graph`", args.json)
    flags, caveats = graph_caveats(fresh)
    command = f"ctx code {args.target}"
    omitted, note = 0, caveats
    with closing(conn):
        path = repo_path(store, args.target)
        outline = query.file_outline(conn, path)
        if outline is not None and args.kind is None:
            blocks = [_file_block(outline), *map(definition_block, outline.definitions)]
        elif outline is None and (Path.cwd() / args.target).is_file():
            blocks = []
            note = "\n".join(x for x in (
                f"{path} is not in the graph: Git ignores it or its language is not supported.",
                caveats) if x)
        else:
            if outline is not None:
                found = [d for d in outline.definitions if d.kind == args.kind]
            else:
                found = query.definitions(conn, args.target, kind=args.kind,
                                          limit=None if args.full else COUNT_UP_TO)
            limit = args.limit or (None if args.full else DEFAULT_LIMIT)
            shown = found if limit is None else found[:limit]
            omitted = len(found) - len(shown)
            blocks = [definition_block(s) for s in shown]
    return Reply(render(command, blocks, full=args.full, as_json=args.json, omitted=omitted,
                        hint="Rerun with --limit N or --full.", note=note,
                        meta={"target": args.target, "flags": flags}),
                 results=len(blocks), flags=flags)
