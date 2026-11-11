"""`ctx rebuild [--index | --graph]`: delete the search index and the code graph and build them again."""

import json

from wscontext.cli.output import Reply, error
from wscontext.config import Config
from wscontext.store.paths import StorePaths


def add_arguments(parser) -> None:
    only = parser.add_mutually_exclusive_group()
    only.add_argument("--index", action="store_true", help="only the search index")
    only.add_argument("--graph", action="store_true", help="only the code graph")


def run(args, store: StorePaths, config: Config) -> Reply:
    out: dict = {}
    lines: list[str] = []
    if not args.graph:
        from wscontext.index import build
        result = build.rebuild(store)
        if result["locked"]:
            return error("another process is updating the index; try again shortly", args.json)
        out.update(result)
        lines.append(f"Index rebuilt: {result['sessions_indexed']} sessions, "
                     f"{result['commits_indexed']} commits indexed.")
        if result["pending_commits"]:
            lines.append(f"{result['pending_commits']} commits are not in this clone yet and "
                         "will be indexed once they are.")
    if not args.index:
        import sqlite3

        from wscontext.gitutil import GitError
        from wscontext.graph import build as graph_build
        try:
            fresh = graph_build.rebuild(store, config)
        except (GitError, OSError, sqlite3.Error) as e:
            return error(f"the code graph could not be rebuilt: {e}", args.json)
        if fresh.busy:
            return error("another process is updating the graph; try again shortly", args.json)
        out["graph"] = {"files_parsed": fresh.parsed, "parse_errors": list(fresh.parse_errors),
                        "unavailable": list(fresh.unavailable), "complete": fresh.complete}
        lines.append(f"Graph rebuilt: {fresh.parsed} files parsed"
                     + (f", {len(fresh.parse_errors)} with parse errors" if fresh.parse_errors else "")
                     + ".")
        if fresh.unavailable:
            lines.append("No parser for " + ", ".join(fresh.unavailable)
                         + "; their files are not in the graph.")
    if args.json:
        return Reply(json.dumps({"command": "ctx rebuild", **out}, indent=2))
    return Reply("\n".join(lines))
