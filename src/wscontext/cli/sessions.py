"""`ctx sessions`: captured sessions, filtered by branch, author, date or file."""

from pathlib import Path

from wscontext.cli.data import list_sessions, parse_since, parse_time
from wscontext.cli.output import Block, Reply, error, render
from wscontext.config import Config
from wscontext.history import sync
from wscontext.store.paths import StorePaths
from wscontext.store.records import SessionRecord

DEFAULT_LIMIT = 20
FILES_SHOWN = 5


def add_arguments(parser) -> None:
    parser.add_argument("--branch", help="only sessions that started on this branch")
    parser.add_argument("--author", help="only sessions whose author contains this text")
    parser.add_argument("--since", help="only sessions started since a date or a duration like 7d")
    parser.add_argument("--file", help="only sessions that edited this file")
    parser.add_argument("--pending", action="store_true",
                        help="only sessions the next share would send")


def _repo_path(store: StorePaths, path: str) -> str:
    try:
        return (Path.cwd() / path).resolve().relative_to(store.root.resolve()).as_posix()
    except ValueError:
        return path


def _block(rec: SessionRecord, pending: bool | None) -> Block:
    tools = ", ".join(f"{k} {n}" for k, n in sorted(rec.tool_calls.items()))
    files = rec.files_touched
    shown = ", ".join(files[:FILES_SHOWN]) + (f" (+{len(files) - FILES_SHOWN})"
                                              if len(files) > FILES_SHOWN else "")
    body = [f"{len(rec.turns)} turns" + (f"; tools: {tools}" if tools else "")
            + ("" if rec.ended_at else "; still open or not ended cleanly")]
    if files:
        body.append(f"files: {shown}")
    if pending:
        body.append("would be sent on the next share")
    return Block("session", rec.session_id,
                 f"{rec.branch or 'unknown branch'} by {rec.author}",
                 (rec.started_at or "")[:10] or None, body, [f"ctx show {rec.session_id}"],
                 {"author": rec.author, "branch": rec.branch, "started_at": rec.started_at,
                  "ended_at": rec.ended_at, "turns": len(rec.turns),
                  "tool_calls": rec.tool_calls, "ctx_calls": rec.ctx_calls,
                  "files_touched": files, "pending": pending})


def run(args, store: StorePaths, config: Config) -> Reply:
    found = list_sessions(store)
    if args.branch:
        found = [r for r in found if r.branch == args.branch]
    if args.author:
        found = [r for r in found if args.author.lower() in r.author.lower()]
    if args.since:
        try:
            since = parse_since(args.since)
        except ValueError as e:
            return error(str(e), args.json)
        found = [r for r in found if (parse_time(r.started_at) or since) >= since]
    if args.file:
        want = _repo_path(store, args.file)
        found = [r for r in found if want in r.files_touched]
    shared = config.sync.remote is not None
    pending = set(sync.pending(store, config))
    if args.pending:
        found = [r for r in found if r.session_id in pending]
    limit = args.limit or (len(found) if args.full else DEFAULT_LIMIT)
    shown = found[:limit]
    blocks = [_block(r, r.session_id in pending if shared else None) for r in shown]
    return Reply(render("ctx sessions", blocks, full=args.full, as_json=args.json,
                        omitted=len(found) - len(shown), hint="Rerun with --limit N or --full.",
                        meta={"sharing_configured": shared, "total": len(found)}))
