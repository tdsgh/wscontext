"""`ctx import`: preview past Claude Code sessions and Markdown documents, then import a choice.

The preview writes nothing. Every form that writes needs `--offer TOKEN` from a preview, so the
numbers cannot shift between looking and choosing (D12).
"""

import json

from wscontext.capture import importer
from wscontext.capture.importer import OfferedDocument, OfferedSession
from wscontext.capture.state import git_value
from wscontext.cli.output import Block, Reply, error, render
from wscontext.config import Config
from wscontext.store.paths import StorePaths

NOT_SET_UP = "wscontext is not set up here; run `ctx init` first"
STALE = "the list changed; run `ctx import` again"
COMMIT_REMINDER = (".wscontext/config.toml changed; commit it so every clone searches the "
                   "same documents.")
SYNC_NOTE = "Sessions and links go out with the next `git push` or `ctx sync`."


def add_arguments(parser) -> None:
    parser.add_argument("--declined", action="store_true",
                        help="also list items declined earlier, marked, at the end")
    parser.add_argument("--offer", metavar="TOKEN", help="the token a preview printed")
    chosen = parser.add_mutually_exclusive_group()
    chosen.add_argument("--all", action="store_true", help="import everything offered")
    chosen.add_argument("--sessions", action="store_true", help="import every offered session")
    chosen.add_argument("--documents", action="store_true", help="import every offered document")
    chosen.add_argument("--take", metavar="N,N-N", help="import these item numbers")
    parser.add_argument("--decline", metavar="N,N-N",
                        help="never offer these item numbers again")


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'s' * (n != 1)}"


def _numbers(text: str, size: int, option: str) -> list[int]:
    found: list[int] = []
    for part in text.split(","):
        low, dash, high = part.strip().partition("-")
        try:
            span = range(int(low), int(high) + 1) if dash else [int(low)]
        except ValueError:
            raise ValueError(f"{option}: {part!r} is not a number or a range like 5-7") from None
        found += span
    for n in found:
        if not 1 <= n <= size:
            raise ValueError(f"{option}: no item {n}; the list has {size}")
    if len(set(found)) != len(found):
        raise ValueError(f"{option}: an item is listed twice")
    return found


def _blocks(offer: importer.Offer) -> list[Block]:
    blocks: list[Block] = []
    number = 0
    for item in offer.items:
        number += 1
        mark = ["declined earlier"] if item in offer.declined else []
        if isinstance(item, OfferedSession):
            sid = item.transcript.session_id
            body = [*mark, f"session {sid}", _count(item.prompts, "prompt")]
            if item.folder:
                body.append(f"folder: {item.folder}")
            body += [f"commit {sha[:7]} {subject}".rstrip() for sha, subject in item.commits]
            blocks.append(Block("session", str(number), item.title,
                                (item.started_at or "")[:10] or None, body,
                                extra={"number": number, "session_id": sid, "prompts": item.prompts,
                                       "folder": item.folder, "declined": bool(mark),
                                       "commits": [{"sha": sha, "subject": subject}
                                                   for sha, subject in item.commits]}))
        else:
            group = "Suggested" if item.group == "suggested" else "Other"
            blocks.append(Block("document", str(number), item.path, None, [*mark, group],
                                extra={"number": number, "path": item.path, "group": item.group,
                                       "declined": bool(mark)}))
    for u in offer.unreadable:
        blocks.append(Block("unreadable", u.path.name, "cannot be imported", u.date or None,
                            [f"file: {u.path}"], extra={"path": str(u.path)}))
    return blocks


def _note(store: StorePaths, offer: importer.Offer, command: str) -> str:
    lines = []
    c = offer.cleanup
    if c.oldest:
        where = f"set in {c.source}" if c.source else "the default"
        lines.append(f"Claude Code deletes transcripts after {c.days} days ({where}). The oldest "
                     f"one here is from {c.oldest} and goes about {c.deleted_on}.")
    if offer.in_use:
        lines.append(f"{_count(offer.in_use, 'session')} in use right now "
                     f"{'was' if offer.in_use == 1 else 'were'} left out.")
    branch = git_value(store, "rev-parse", "--abbrev-ref", "HEAD") or "HEAD"
    lines.append(f"Commits on {branch} with no session: {offer.unlinked_now} now, "
                 f"{offer.unlinked_after} after importing every session above.")
    flag = " --declined" if offer.declined else ""
    t = offer.token
    lines += ["To import:",
              f"  {command}{flag} --offer {t} --all",
              f"  {command}{flag} --offer {t} --sessions   (or --documents)",
              f"  {command}{flag} --offer {t} --take 1,3,5-7",
              f"  {command}{flag} --offer {t} --decline 2,4"]
    return "\n".join(lines)


def _summary(config: Config, summary: importer.Summary, as_json: bool) -> Reply:
    failed = bool(summary.errors)
    if as_json:
        return Reply(json.dumps({
            "command": "ctx import", "sessions": summary.sessions, "commits": summary.commits,
            "documents": summary.documents, "declined": summary.declined,
            "errors": summary.errors, "sync_note": SYNC_NOTE if config.sync.remote else None,
            "config_changed": bool(summary.documents)}, indent=2), int(failed), report=True)
    lines = [f"Imported {_count(len(summary.sessions), 'session')}, linked "
             f"{_count(len(summary.commits), 'commit')}, added "
             f"{_count(len(summary.documents), 'document')}."]
    if summary.declined:
        lines.append(f"Declined {_count(len(summary.declined), 'item')}; "
                     f"{'it is' if len(summary.declined) == 1 else 'they are'} not offered again.")
    lines += [f"error: {key}: {text}" for key, text in summary.errors.items()]
    if summary.sessions and config.sync.remote:
        lines.append(SYNC_NOTE)
    if summary.documents:
        lines.append(COMMIT_REMINDER)
    return Reply("\n".join(lines), int(failed), report=True)


def run(args, store: StorePaths, config: Config) -> Reply:
    if not (store.root / ".wscontext" / "config.toml").is_file():
        return error(NOT_SET_UP, args.json)
    writes = args.all or args.sessions or args.documents or args.take or args.decline
    if writes and not args.offer:
        return error("choosing items needs --offer TOKEN from a preview", args.json)
    if args.offer and not writes:
        return error("--offer needs --all, --sessions, --documents, --take or --decline",
                     args.json)
    offer = importer.find(store, config, include_declined=args.declined)
    if not writes:
        if not offer.items:
            cannot = [str(u.path) for u in offer.unreadable]
            text = "Nothing new to import."
            if cannot:
                text += f" {_count(len(cannot), 'transcript')} cannot be read by this version."
            return Reply(json.dumps({"command": "ctx import", "results": [], "note": text,
                                     "unreadable": cannot, "token": offer.token}, indent=2)
                         if args.json else text, results=0)
        meta = {"token": offer.token, "in_use": offer.in_use,
                "unlinked_now": offer.unlinked_now, "unlinked_after": offer.unlinked_after,
                "cleanup": {"days": offer.cleanup.days,
                            "source": str(offer.cleanup.source) if offer.cleanup.source else None,
                            "oldest": offer.cleanup.oldest, "deleted_on": offer.cleanup.deleted_on}}
        text = render("ctx import", _blocks(offer), full=True, as_json=args.json,
                      note=_note(store, offer, "ctx import"), meta=meta)
        return Reply(text, results=len(offer.items))
    if args.offer != offer.token:
        return error(STALE, args.json)
    items = offer.items
    try:
        declined = [items[n - 1] for n in _numbers(args.decline, len(items), "--decline")] \
            if args.decline else []
        if args.take:
            taken = [items[n - 1] for n in _numbers(args.take, len(items), "--take")]
        else:
            taken = [i for i in offer.sessions + offer.documents
                     if (args.all or args.sessions and isinstance(i, OfferedSession)
                         or args.documents and isinstance(i, OfferedDocument))
                     and i not in declined]
        if {i.key for i in taken} & {i.key for i in declined}:
            raise ValueError("an item cannot be both taken and declined")
    except ValueError as e:
        return error(str(e), args.json)
    return _summary(config, importer.run(store, config, offer, taken, declined), args.json)
