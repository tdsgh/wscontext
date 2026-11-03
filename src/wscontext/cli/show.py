"""`ctx show <session>`: one captured session, page by page."""

from wscontext.cli.data import find_session, transcript_turns
from wscontext.cli.output import Block, Reply, error, quote, render
from wscontext.config import Config
from wscontext.store.paths import StorePaths

PAGE_TURNS = 10


def add_arguments(parser) -> None:
    parser.add_argument("session", help="session ID or a unique prefix of it")
    parser.add_argument("--page", type=int, default=1, help="page number, from 1")


def run(args, store: StorePaths, config: Config) -> Reply:
    record = find_session(store, args.session)
    if record is None:
        return error(f"no session {args.session!r}", args.json)
    if isinstance(record, list):
        return error(f"{args.session!r} matches several sessions: {', '.join(record)}",
                     args.json, candidates=record)
    turns = transcript_turns(store, record)
    size = args.limit or (len(turns) or 1 if args.full else PAGE_TURNS)
    pages = max(1, -(-len(turns) // size))
    if not 1 <= args.page <= pages:
        return error(f"page {args.page} does not exist: the session has {pages}", args.json)
    sid = record.session_id
    window = turns[(args.page - 1) * size: args.page * size]
    blocks = []
    if args.page == 1:
        blocks.append(Block("session", sid, f"{record.branch or 'unknown branch'} by {record.author}",
                            (record.started_at or "")[:10] or None,
                            [f"{len(turns)} turns; ended {record.ended_at or 'not recorded'}"],
                            extra={"author": record.author, "branch": record.branch,
                                   "started_at": record.started_at, "ended_at": record.ended_at,
                                   "head_start": record.head_start, "head_end": record.head_end,
                                   "files_touched": record.files_touched,
                                   "subagents": record.subagents}))
    for t in window:
        tools = ", ".join(f"{k} {n}" for k, n in sorted(t.tool_calls.items()))
        body = quote(t.prompt).split("\n") if t.prompt is not None else [
            "prompt unavailable: transcript not captured or format not recognised"]
        if tools:
            body.append(f"tools: {tools}")
        blocks.append(Block("turn", f"{sid}#{t.index}", "", (t.started_at or "")[:10] or None, body,
                            extra={"session_id": sid, "turn": t.index, "started_at": t.started_at,
                                   "prompt": t.prompt, "tool_calls": t.tool_calls,
                                   "tokens": t.tokens}))
    nxt = f"ctx show {sid} --page {args.page + 1}"
    return Reply(render(f"ctx show {sid}", blocks, full=args.full, as_json=args.json,
                        hint="Use --limit N for smaller pages or --full for everything.",
                        note=f"Page {args.page} of {pages}." + (f" Next: `{nxt}`"
                                                              if args.page < pages else ""),
                        meta={"session_id": sid, "page": args.page, "pages": pages,
                              "next": nxt if args.page < pages else None}))
