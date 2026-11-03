"""`ctx show <session>`: one captured session, in full or as turn previews, page by page.

The default view renders every turn's items (prompt, assistant text, tool calls and results,
subagent references) from the store's already-redacted raw copy. Nothing rendered is persisted.
`--turns` lists one preview per turn instead. `--subagent <id>` renders that subagent's
transcript the same way.
"""

from wscontext.adapters.base import Item
from wscontext.adapters.claude_code import ClaudeCodeAdapter
from wscontext.cli.data import find_session, transcript_turns
from wscontext.cli.output import Block, Reply, block_budget, error, estimate_tokens, quote, render
from wscontext.config import Config
from wscontext.store.paths import StorePaths, is_safe_name

PAGE_TURNS = 10
CHUNK_CHARS = 1000  # a longer item is split into parts of about this size, so a page can end inside it


def add_arguments(parser) -> None:
    parser.add_argument("session", help="session ID or a unique prefix of it")
    parser.add_argument("--page", type=int, default=1, help="page number, from 1")
    parser.add_argument("--turns", action="store_true",
                        help="one preview per turn instead of the full content")
    parser.add_argument("--subagent", metavar="ID", help="show this subagent's transcript instead")


def _chunks(text: str) -> list[list[str]]:
    """The text's lines grouped into parts of about CHUNK_CHARS, long lines cut."""
    parts: list[list[str]] = [[]]
    used = 0
    for line in text.splitlines() or [""]:
        for i in range(0, max(len(line), 1), CHUNK_CHARS):
            piece = line[i:i + CHUNK_CHARS]
            if used + len(piece) + 3 > CHUNK_CHARS and parts[-1]:
                parts.append([])
                used = 0
            parts[-1].append(piece)
            used += len(piece) + 3
    return parts


def _item_blocks(store: StorePaths, sid: str, scope: str, turn, n: int,
                 item: Item) -> list[Block]:
    """`scope` prefixes block IDs: the session ID, or `<session>/<agent>` in a subagent view."""
    date = (turn.started_at or "")[:10] or None
    ident = f"{scope}#{turn.index}.{n}"
    extra = {"session_id": sid, "turn": turn.index, "item": n, "kind": item.kind,
             "name": item.name, "tool_id": item.tool_id, "size": item.size,
             "source_truncated": item.truncated, "agent_id": item.agent_id}
    if item.kind == "subagent":
        copy = store.raw / "subagents" / sid / f"{item.agent_id}.jsonl"
        if item.agent_id and is_safe_name(item.agent_id) and copy.is_file():
            link = f"ctx show {sid} --subagent {item.agent_id}"
            extra["command"] = link
            return [Block("subagent", ident, "", date, [f"subagent {item.agent_id}"], [link],
                          extra=extra)]
        extra["command"] = None
        return [Block("subagent", ident, "", date,
                      [f"subagent {item.agent_id}: no transcript copy captured"], extra=extra)]
    text = item.text
    if item.kind == "tool_call":
        text = f"{item.name or 'tool'}: {item.text}" if item.text else item.name or "tool"
    parts = _chunks(text)
    blocks = []
    for k, lines in enumerate(parts, 1):
        title = f"(part {k} of {len(parts)})" if len(parts) > 1 else ""
        body = quote("\n".join(lines)).split("\n")
        if item.kind == "tool_result" and k == len(parts) and item.truncated:
            body.append(f"[result cut at {len(item.text)} of {item.size} characters]")
        kind = {"text": "reply"}.get(item.kind, item.kind)
        blocks.append(Block(kind, ident, title, date, body, extra={**extra, "part": k,
                                                                     "parts": len(parts)}))
    return blocks


def _pack(blocks: list[Block], budget: int) -> list[list[Block]]:
    """Consecutive blocks grouped into pages that each fit the budget."""
    pages: list[list[Block]] = [[]]
    used = 0
    for block in blocks:
        cost = estimate_tokens(block.text()) + 1
        if used + cost > budget and pages[-1]:
            pages.append([])
            used = 0
        pages[-1].append(block)
        used += cost
    return pages


def run(args, store: StorePaths, config: Config) -> Reply:
    record = find_session(store, args.session)
    if record is None:
        return error(f"no session {args.session!r}", args.json)
    if isinstance(record, list):
        return error(f"{args.session!r} matches several sessions: {', '.join(record)}",
                     args.json, candidates=record)
    turns = transcript_turns(store, record)
    sid = record.session_id
    session = Block("session", sid, f"{record.branch or 'unknown branch'} by {record.author}",
                    (record.started_at or "")[:10] or None,
                    [f"{len(turns)} turns; ended {record.ended_at or 'not recorded'}"],
                    extra={"author": record.author, "branch": record.branch,
                           "started_at": record.started_at, "ended_at": record.ended_at,
                           "head_start": record.head_start, "head_end": record.head_end,
                           "files_touched": record.files_touched,
                           "subagents": record.subagents})
    if args.subagent:
        return _subagent(args, store, sid, turns)
    if not args.turns and any(t.items for t in turns):
        return _full(args, store, sid, sid, f"ctx show {sid}", session, turns)
    size = args.limit or (len(turns) or 1 if args.full else PAGE_TURNS)
    pages = max(1, -(-len(turns) // size))
    if not 1 <= args.page <= pages:
        return error(f"page {args.page} does not exist: the session has {pages}", args.json)
    window = turns[(args.page - 1) * size: args.page * size]
    blocks = [session] if args.page == 1 else []
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


def _full(args, store: StorePaths, sid: str, scope: str, command: str, head: Block,
          turns, noun: str = "session") -> Reply:
    blocks = [head]
    for t in turns:
        for n, item in enumerate(t.items):
            blocks += _item_blocks(store, sid, scope, t, n, item)
    pages = [blocks] if args.full else _pack(blocks, block_budget(command))
    if not 1 <= args.page <= len(pages):
        return error(f"page {args.page} does not exist: the {noun} has {len(pages)}", args.json)
    nxt = f"{command} --page {args.page + 1}" if args.page < len(pages) else None
    return Reply(render(command, pages[args.page - 1], full=args.full, as_json=args.json,
                        note=f"Page {args.page} of {len(pages)}."
                             + (f" Next: `{nxt}`" if nxt else f" End of {noun}."),
                        meta={"session_id": sid, "page": args.page, "pages": len(pages),
                              "next": nxt}))


def _subagent(args, store: StorePaths, sid: str, turns) -> Reply:
    """The full view of one subagent transcript, from the store's redacted copy."""
    agent = args.subagent
    folder = store.raw / "subagents" / sid
    copy = folder / f"{agent}.jsonl"
    if not (is_safe_name(agent) and copy.is_file()):
        known = sorted({p.stem for p in folder.glob("*.jsonl")}
                       | {i.agent_id for t in turns for i in t.items if i.agent_id})
        return error(f"no subagent {agent!r} in session {sid}; subagents: "
                     + (", ".join(known) or "none"), args.json, subagents=known)
    parsed = ClaudeCodeAdapter().parse(copy, sidechain=True)
    if not parsed.supported or not parsed.turns:
        return error(f"subagent {agent!r} transcript is empty or in an unrecognised format",
                     args.json)
    first = parsed.turns[0]
    head = Block("subagent", f"{sid}/{agent}", f"of session {sid}", (first.started_at or "")[:10] or None,
                 [f"{len(parsed.turns)} turns"], [f"ctx show {sid}"],
                 extra={"session_id": sid, "agent_id": agent})
    return _full(args, store, sid, f"{sid}/{agent}", f"ctx show {sid} --subagent {agent}", head,
                 parsed.turns, "transcript")
