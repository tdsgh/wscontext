"""`ctx stats [data|turns|queries|hooks|session <id>] [--since <date|duration>]`: terminal report."""

from wscontext.cli.data import find_session, parse_since
from wscontext.cli.output import Block, Reply, error, render
from wscontext.config import Config
from wscontext.stats import data, hooks, queries, render as views, turns
from wscontext.store.paths import StorePaths

VIEWS = ["data", "turns", "queries", "hooks", "session", "code"]
DEFAULT_CALLS = 20


def add_arguments(parser) -> None:
    parser.add_argument("view", nargs="?", default="data", choices=VIEWS,
                        help="what to report (default: data)")
    parser.add_argument("session_id", nargs="?", help="session ID or prefix, for `session`")
    parser.add_argument("--since", help="only the window since a date or a duration like 7d")


def run(args, store: StorePaths, config: Config) -> Reply:
    since = None
    if args.since:
        try:
            since = parse_since(args.since)
        except ValueError as e:
            return error(str(e), args.json)
    command = f"ctx stats {args.view}"
    meta = {"view": args.view, "since": since.isoformat() if since else None}
    if args.view == "code":
        blocks = [Block("stats", "code", "available from stage 3")]
    elif args.view == "data":
        blocks = views.data_blocks(data.collect(store, config, since))
    elif args.view == "turns":
        blocks = views.turns_blocks(turns.summarize(data.window_sessions(store, since), since))
    elif args.view == "hooks":
        blocks = views.hooks_blocks(hooks.summarize(store, since))
    elif args.view == "queries":
        calls = queries.join_calls(store, since)
        sessions = data.window_sessions(store, since)
        summary = queries.summarize(calls, queries.ctx_first(store, sessions),
                                    queries.adoption(sessions))
        blocks = views.queries_blocks(summary, calls,
                                      len(calls) if args.full else args.limit or DEFAULT_CALLS)
    else:
        if not args.session_id:
            return error("`ctx stats session` needs a session ID", args.json)
        record = find_session(store, args.session_id)
        if record is None:
            return error(f"no session matches {args.session_id!r}", args.json)
        if isinstance(record, list):
            return error(f"{args.session_id!r} matches several sessions: {', '.join(record)}",
                         args.json, candidates=record)
        command = f"ctx stats session {record.session_id}"
        calls = [c for c in queries.join_calls(store, since) if c["session_id"] == record.session_id]
        blocks = views.session_blocks(turns.session_detail(record), calls,
                                      queries.first_tool(store, record),
                                      data.session_volume(store, record))
    return Reply(render(command, blocks, full=args.full, as_json=args.json,
                        hint="Rerun with --full.", meta=meta))
