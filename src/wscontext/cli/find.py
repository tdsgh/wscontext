"""`ctx find <text>`: past sessions and commits that discuss a topic, best match first."""

import json
import sqlite3
import time

from wscontext.cli.data import parse_since, repo_path
from wscontext.cli.output import Block, Reply, error, quote, render
from wscontext.config import Config
from wscontext.index import build
from wscontext.index.db import IndexUnavailable, connect
from wscontext.index.search import search
from wscontext.store.paths import StorePaths

DEFAULT_LIMIT = 10
FULL_LIMIT = 50
COUNT_UP_TO = 100  # how many matches are counted so the answer can say how many were left out
OFF_MESSAGE = ("ctx find: search is switched off for this repository "
               "([retrieval] enabled = false in .wscontext/config.toml).")


def add_arguments(parser) -> None:
    parser.add_argument("text", nargs="+", help="what to look for, in plain words")
    parser.add_argument("--branch", help="only work done on this branch")
    parser.add_argument("--author", help="only work by this author")
    parser.add_argument("--since", help="only work since a date or a duration like 7d")
    parser.add_argument("--file", help="only work that touched this file")
    parser.add_argument("--kind", choices=["session", "commit"], help="only sessions or only commits")


def _fetch_pending(store: StorePaths, config: Config) -> bool:
    """A sync remote is set and its history was not fetched within the throttle window."""
    if config.sync.remote is None:
        return False
    try:
        age = time.time() - float((store.state / "last_fetch").read_text())
    except (OSError, ValueError):
        return True
    return age > config.sync.fetch_throttle_seconds


def _block(hit) -> Block:
    commit = hit.kind == "commit"
    who = f"{hit.branch or 'unknown branch'} by {hit.author or 'unknown'}"
    body = [who, *quote(hit.snippet).split("\n")]
    if hit.active:
        body.append("session still running: not final")
    links = [f"git show {hit.ref[:12]}"] if commit else [f"ctx show {hit.ref}"]
    if hit.session_id:
        links.append(f"ctx show {hit.session_id}")
    return Block(hit.kind, hit.ref[:12] if commit else hit.ref, hit.title, (hit.date or "")[:10] or None,
                 body, list(dict.fromkeys(links)),
                 {"session_id": hit.session_id, "turn": hit.turn, "branch": hit.branch,
                  "author": hit.author, "snippet": hit.snippet, "score": hit.score,
                  "active": hit.active, "sha": hit.ref if commit else None})


def run(args, store: StorePaths, config: Config) -> Reply:
    if not config.retrieval.enabled:
        if args.json:
            return Reply(json.dumps({"command": "ctx find", "enabled": False,
                                     "message": OFF_MESSAGE}, indent=2), results=0)
        return Reply(OFF_MESSAGE, results=0)
    since = None
    if args.since:
        try:
            since = parse_since(args.since).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError as e:
            return error(str(e), args.json)
    gap = build.refresh(store)
    try:
        conn = connect(store, readonly=True)
    except (IndexUnavailable, sqlite3.Error):
        return error("no search index yet; run `ctx rebuild`", args.json)
    limit = args.limit or (FULL_LIMIT if args.full else DEFAULT_LIMIT)
    try:
        hits = search(conn, " ".join(args.text), branch=args.branch, author=args.author,
                      since=since, file=repo_path(store, args.file) if args.file else None,
                      kind=args.kind, limit=max(limit + 1, COUNT_UP_TO))
    except sqlite3.Error as e:
        return error(f"search failed: {e}", args.json)
    finally:
        conn.close()
    shown = hits[:limit]
    flags, notes = [], []
    if _fetch_pending(store, config):
        flags.append("pending-fetch")
        notes.append("History from the sync remote may not be fetched yet; this answer may be incomplete.")
    if any(h.active for h in shown):
        flags.append("active-session")
        notes.append("A session in these results is still running; it may hold more than shown.")
    if gap["locked"]:
        flags.append("index-busy")
        notes.append("The index was being updated by another process; the newest history may be missing.")
    elif gap["lag"] > 0:
        flags.append("index-behind")
        notes.append(f"{gap['lag']} commits are not indexed yet (not in this clone); they cannot match.")
    reply = render("ctx find", [_block(h) for h in shown], full=args.full, as_json=args.json,
                   omitted=len(hits) - len(shown), hint="Rerun with --limit N or --full.",
                   note=" ".join(notes), meta={"query": " ".join(args.text), "flags": flags})
    return Reply(reply, results=len(shown), flags=flags)
