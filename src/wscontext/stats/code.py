"""`ctx stats code`: what the code graph holds. Read-only: never refreshes, locks or starts a worker."""

import sqlite3

from wscontext.cli.output import Block
from wscontext.config import Config
from wscontext.graph import build, db
from wscontext.graph.languages import REGISTRY, load_parts
from wscontext.stats.render import size, table
from wscontext.store.paths import StorePaths


def collect(store: StorePaths, config: Config) -> dict:
    """Languages supported, switched on and unavailable; the graph summary (None without a usable
    graph); files changed since the graph last read them (None: no graph, or unknown)."""
    _, unavailable = load_parts(config.graph.languages)
    try:
        conn = db.connect(store, readonly=True)
    except db.GraphUnavailable:
        graph = None
    else:
        try:
            graph = db.summary(conn)
        except sqlite3.DatabaseError:
            graph = None
        finally:
            conn.close()
    return {"languages": {"supported": sorted(REGISTRY), "enabled": list(config.graph.languages),
                          "unavailable": unavailable},
            "graph": graph,
            "changed_files": build.changed_files(store, config) if graph else None}


def _last(label: str, run: dict | None) -> str:
    return f"{label}: never" if run is None else \
        f"{label}: {run['at']}, {run['ms']} ms, {run['parsed']} files parsed"


def render(figures: dict) -> list[Block]:
    langs, graph = figures["languages"], figures["graph"]
    body = [f"languages supported: {', '.join(langs['supported']) or 'none'}",
            f"switched on: {', '.join(langs['enabled']) or 'none'}",
            f"unavailable: {', '.join(langs['unavailable']) or 'none'}"]
    if graph is None:
        body.append("no graph yet: it is built by the first `ctx code`, `ctx impact` or "
                    "`ctx rebuild --graph`")
    else:
        rows = [[name, c["files"], c["symbols"], c["imports"], c["calls"]]
                for name, c in graph["by_language"].items()]
        status = graph["by_status"]
        changed = figures["changed_files"]
        body += [f"files by status: ok {status['ok']}, error {status['error']}, "
                 f"skipped {status['skipped']}",
                 *(table(["language", "files", "definitions", "imports", "calls"], rows)
                   if rows else ["no files in the graph"]),
                 _last("last build", graph["last_build"]),
                 _last("last refresh", graph["last_refresh"]),
                 f"files changed since the graph last read them: "
                 f"{'unknown' if changed is None else changed}",
                 f"size on disk: {size(graph['bytes'])}"]
    return [Block("stats", "code", "code graph", None, body, [], figures)]
