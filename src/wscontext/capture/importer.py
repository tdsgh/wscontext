"""Past work for `ctx import`: Claude Code sessions still on this machine, the commits they
made, and the repository's Markdown documents.

`find` reads only and writes nothing. `run` is the only writer on behalf of `ctx import`: each
session lands whole, as live capture stores one, and nothing is pushed.
"""

import hashlib
import math
import os
import re
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePath

from wscontext.adapters import claude_home
from wscontext.adapters.base import ToolUse
from wscontext.adapters.claude_code import ClaudeCodeAdapter, owner_ids, prompt_without_wrappers
from wscontext.adapters.claude_home import Transcript
from wscontext.capture.kinds import ctx_call_key, ctx_invocations, tool_kind
from wscontext.capture.state import git_author, parse_time, read_json, update_json
from wscontext.capture.worker import (_raw_name, _read_record, _session_lock, copy_reduced,
                                      copy_subagents, stored_sizes, turn_records)
from wscontext.config import Config, load_config, set_document_paths
from wscontext.githooks.handlers import commit_links
from wscontext.githooks.worker import _diff_stats
from wscontext.gitutil import GitError, git
from wscontext.history.branch import commit_paths, ls_blobs, read_blob, read_tip
from wscontext.index import build as index_build
from wscontext.logs import now_iso
from wscontext.redact.account import learn_account_file, with_account_ids
from wscontext.redact.redactor import add_digests, redact_text
from wscontext.redact.rules import UUID, account_digest
from wscontext.store.locks import FileLock
from wscontext.store.paths import StorePaths, is_safe_name
from wscontext.store.records import (CheckpointRecord, RecordError, SessionRecord,
                                     checkpoint_path, read_checkpoint, write_checkpoint,
                                     write_session)

# D7: a Bash command that runs `git commit`, and Git's summary line naming the commit it made
GIT_COMMIT = re.compile(r"\bgit\b[^\n|;&]*\bcommit\b")
SUMMARY = re.compile(r"^\[(?:detached HEAD|[^\s\]]+)(?: \(root-commit\))? ([0-9a-f]{7,64})\] ",
                     re.M)
EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
SUGGESTED_FOLDERS = {"docs", "doc"}
SUGGESTED_WORDS = ("adr", "architecture", "design", "decision", "rfc")


@dataclass
class OfferedSession:
    transcript: Transcript
    title: str  # redacted
    prompts: int
    folder: str | None  # a subfolder relative to the root, or a worktree; None for the root
    worktree: str  # the record's worktree
    started_at: str | None
    commits: list[tuple[str, str]] = field(default_factory=list)  # planned: (sha, subject)
    # every commit its `git commit` output names, with the result's time
    named: list[tuple[str, float]] = field(default_factory=list, repr=False)

    @property
    def key(self) -> str:
        return f"session:{self.transcript.session_id}"


@dataclass
class OfferedDocument:
    path: str
    group: str  # "suggested" or "other"

    @property
    def key(self) -> str:
        return f"document:{self.path}"


@dataclass
class Unreadable:
    date: str
    path: Path


@dataclass
class Cleanup:
    days: int
    source: Path | None  # the settings file the period came from; None for the default
    oldest: str | None  # the oldest offered transcript's modification date
    deleted_on: str | None  # about when Claude Code deletes it


@dataclass
class Offer:
    sessions: list[OfferedSession]
    unreadable: list[Unreadable]
    in_use: int
    documents: list[OfferedDocument]
    declined: list  # OfferedSession and OfferedDocument items, filled only when asked
    unlinked_now: int
    unlinked_after: int
    cleanup: Cleanup
    token: str
    # held sessions' commit names, for planning links again at import time
    held_named: dict[str, list[tuple[str, float]]] = field(default_factory=dict, repr=False)

    @property
    def items(self) -> list:
        """Everything numbered, in preview order: item n is `items[n - 1]`."""
        return [*self.sessions, *self.documents, *self.declined]


@dataclass
class Summary:
    sessions: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)
    declined: list[str] = field(default_factory=list)  # item keys
    errors: dict[str, str] = field(default_factory=dict)  # session ID, "documents" or "index"


def state_path(store: StorePaths) -> Path:
    return store.state / "import.json"


def _declined(store: StorePaths) -> tuple[dict, dict]:
    data = read_json(state_path(store))
    sessions, documents = data.get("declined_sessions"), data.get("declined_documents")
    return (sessions if isinstance(sessions, dict) else {},
            documents if isinstance(documents, dict) else {})


def _norm(path) -> PurePath:
    return PurePath(os.path.normcase(Path(path).resolve()))


def _places(store: StorePaths) -> list[tuple[PurePath, Path]]:
    """The repository root and every worktree Git lists, normalized for comparison."""
    paths = [store.root]
    try:
        out = git("worktree", "list", "--porcelain", cwd=store.root)
        paths += [Path(line[len("worktree "):]) for line in out.splitlines()
                  if line.startswith("worktree ")]
    except GitError:
        pass
    places: dict[PurePath, Path] = {}
    for p in paths:
        places.setdefault(_norm(p), p)
    return list(places.items())


def _belongs(store: StorePaths, cwd: str, places) -> tuple[str, str | None] | None:
    """D2: (the record's worktree, the folder label) when a transcript's working folder is
    the root, a worktree or inside one; else None."""
    here = _norm(cwd)
    inside = [n for n, _ in places if here.is_relative_to(n)]
    if not inside:
        return None
    if Path(cwd).is_dir():
        try:
            top = _norm(git("rev-parse", "--show-toplevel", cwd=Path(cwd)).strip())
        except (GitError, OSError):
            return None
        holder = next(((n, p) for n, p in places if n == top), None)
        if holder is None:
            return None  # a separate repository nested inside
        held_by, worktree = holder[0], str(holder[1])
    else:
        # gone, with the worktree that held it: accepted by path alone
        held_by, worktree = max(inside, key=lambda n: len(n.parts)), cwd
    root = _norm(store.root)
    if held_by != root:
        return worktree, cwd
    return worktree, None if here == root else here.relative_to(root).as_posix()


def _when(ts: str | None) -> float:
    return parse_time(ts) or math.inf


def _commit_names(uses: list[ToolUse]) -> list[tuple[str, float]]:
    """(hex prefix, when) for every commit a `git commit` result names in its summary line."""
    names = []
    for u in uses:
        command = u.input.get("command")
        if u.name == "Bash" and isinstance(command, str) and GIT_COMMIT.search(command) \
                and u.result:
            names += [(m[1], _when(u.timestamp)) for m in SUMMARY.finditer(u.result)]
    return names


def _all_uses(path: Path, parsed) -> list[ToolUse]:
    """The session's tool uses and its subagents'."""
    adapter = ClaudeCodeAdapter()
    uses = adapter.tool_uses(path)
    for ref in parsed.subagents:
        if ref.path is not None:
            uses += adapter.tool_uses(ref.path, sidechain=True)
    return uses


def _resolve(store: StorePaths, prefixes: set[str]) -> dict[str, str]:
    """Prefix -> full SHA, for each prefix that names exactly one commit object."""
    ordered = sorted(prefixes)
    if not ordered:
        return {}
    out = git("cat-file", "--batch-check", cwd=store.root,
              input="".join(f"{p}^{{commit}}\n" for p in ordered))
    found = {}
    for prefix, line in zip(ordered, out.splitlines()):
        fields = line.split()
        if len(fields) == 3 and fields[1] == "commit" and fields[0].startswith(prefix):
            found[prefix] = fields[0]
    return found


def _checkpoints(store: StorePaths, tip_paths: set[str]) -> tuple[set[str], set[str]]:
    """(SHAs with a local checkpoint record, SHAs with one on the local tip)."""
    local = {p.stem for p in (store.records / "checkpoints").glob("*.json")}
    on_tip = {p[len("records/checkpoints/"):-len(".json")] for p in tip_paths
              if p.startswith("records/checkpoints/") and p.endswith(".json")}
    return local, on_tip


def _plan(store: StorePaths, named: dict[str, list[tuple[str, float]]],
          candidates: set[str], tip_paths: set[str]) -> dict[str, str]:
    """D7: SHA -> the candidate session it links to. The earliest result wins, ties go to the
    smaller ID; a commit with a checkpoint record or an `AI-Session` trailer is left alone. A
    local checkpoint off the tip that names one candidate alone is an interrupted import's,
    and stays that candidate's."""
    best: dict[str, tuple[float, str]] = {}
    for sid, names in named.items():
        for sha, when in names:
            if (when, sid) < best.get(sha, (math.inf, "￿")):
                best[sha] = (when, sid)
    if not best:
        return {}
    local, on_tip = _checkpoints(store, tip_paths)
    trailers = {sha for sha, (_, ids) in commit_links(store, sorted(best)).items() if ids}
    links = {}
    for sha, (_, sid) in best.items():
        if sha in local and sha not in on_tip:
            try:
                ids = read_checkpoint(store, sha).session_ids
            except (OSError, RecordError):
                continue
            if len(ids) == 1 and ids[0] in candidates:
                links[sha] = ids[0]
        elif sha not in local and sha not in on_tip and sha not in trailers \
                and sid in candidates:
            links[sha] = sid
    return links


def _subjects(store: StorePaths, shas: list[str]) -> dict[str, tuple[str, str]]:
    """SHA -> (committer date, subject)."""
    if not shas:
        return {}
    out = git("log", "--no-walk=unsorted", "--no-show-signature", "--stdin",
              "--format=%H%x09%cI%x09%s", cwd=store.root, input="".join(f"{s}\n" for s in shas))
    return {f[0]: (f[1], f[2]) for f in (line.split("\t", 2) for line in out.splitlines())
            if len(f) == 3}


def _documents(store: StorePaths, chosen: set[str], declined: dict
               ) -> tuple[list[OfferedDocument], list[OfferedDocument]]:
    """D10: tracked Markdown files not chosen yet, Suggested before Other, each by path;
    and the declined ones."""
    try:
        out = git("ls-files", "-z", "--", ":(icase)*.md", cwd=store.root)
    except GitError:
        return [], []
    offered, refused = [], []
    for path in sorted(set(filter(None, out.split("\0")))):
        *folders, name = path.split("/")
        if name == "CLAUDE.md" or path in chosen:
            continue
        try:
            if (store.root / path).stat().st_size > index_build.DOCUMENT_MAX_BYTES:
                continue
        except OSError:
            continue
        suggested = any(f.lower() in SUGGESTED_FOLDERS for f in folders) or any(
            w in name.lower() for w in SUGGESTED_WORDS)
        doc = OfferedDocument(path, "suggested" if suggested else "other")
        (refused if path in declined else offered).append(doc)
    order = {"suggested": 0, "other": 1}
    return (sorted(offered, key=lambda d: (order[d.group], d.path)),
            sorted(refused, key=lambda d: (order[d.group], d.path)))


def _title(turns, config: Config) -> str:
    """The first prompt as `ctx find` shows it, redacted before it is cut."""
    for t in turns:
        first = prompt_without_wrappers(next((i.text for i in t.items if i.kind == "prompt"), ""))
        if first:
            return redact_text(first, config)[0][:index_build.TITLE_CHARS]
    return ""


def _date(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


def find(store: StorePaths, config: Config, include_declined: bool = False) -> Offer:
    """What `ctx import` offers (D2 to D5, D7, D8, D10, D12). Reads only: writes no file."""
    config = with_account_ids(config, store)
    places = _places(store)
    tip = read_tip(store)
    tip_paths = set(ls_blobs(store, tip)) if tip else set()
    declined_sessions, declined_documents = _declined(store)
    cutoff = time.time() - config.capture.stale_session_minutes * 60
    adapter = ClaudeCodeAdapter()
    offered: list[OfferedSession] = []
    refused: list[OfferedSession] = []
    unreadable: list[Unreadable] = []
    held: list[Transcript] = []
    in_use = 0
    seen: set[str] = set()
    for t in claude_home.transcripts():
        where = _belongs(store, t.cwd, places)
        if where is None or t.session_id in seen:
            continue
        seen.add(t.session_id)
        record = _read_record(store, t.session_id)
        on_tip = f"records/sessions/{t.session_id}.json" in tip_paths
        local = (store.records / "sessions" / f"{t.session_id}.json").exists()
        if on_tip or (local and (record is None or record.imported_at is None)):
            held.append(t)  # D3; a local imported record off the tip is offered again
            continue
        if t.session_id in declined_sessions and not include_declined:
            continue
        if t.mtime > cutoff:
            in_use += 1
            continue
        try:
            data = t.path.read_bytes()
            parsed = adapter.parse(t.path)
            uses = _all_uses(t.path, parsed) if parsed.supported else []
        except OSError:
            continue
        if not parsed.supported or not parsed.turns:
            unreadable.append(Unreadable((t.first_ts or "")[:10] or _date(t.mtime), t.path))
            continue
        shown = add_digests(config, frozenset(
            account_digest(i) for i in owner_ids(data) if UUID.fullmatch(i)))
        stamps = [e.timestamp for e in parsed.events if e.timestamp]
        folder = None if where[1] is None else redact_text(where[1], shown)[0]
        session = OfferedSession(t, _title(parsed.turns, shown), len(parsed.turns), folder,
                                 where[0], stamps[0] if stamps else t.first_ts,
                                 named=_commit_names(uses))
        (refused if t.session_id in declined_sessions else offered).append(session)
    listed = offered + refused
    resolved = _resolve(store, {p for s in listed for p, _ in s.named})
    for s in listed:
        s.named = [(resolved[p], when) for p, when in s.named if p in resolved]
    wanted = {sha for s in listed for sha, _ in s.named}
    held_names: dict[str, list[tuple[str, float]]] = {}
    for t in held:
        # only a held session that may name a planned commit is read in full
        files = [t.path, *(t.path.parent / t.path.stem / "subagents").glob("*.jsonl"),
                 *t.path.parent.glob("agent-*.jsonl")]
        try:
            if any(sha[:7].encode() in f.read_bytes() for f in files for sha in wanted):
                held_names[t.session_id] = _commit_names(_all_uses(t.path, adapter.parse(t.path)))
        except OSError:
            continue
    more = _resolve(store, {p for names in held_names.values() for p, _ in names})
    held_named = {sid: [(more[p], w) for p, w in names if p in more]
                  for sid, names in held_names.items()}
    named = {s.transcript.session_id: s.named for s in listed} | held_named
    links = _plan(store, named, {s.transcript.session_id for s in listed}, tip_paths)
    info = _subjects(store, sorted(links))
    for s in listed:
        sid = s.transcript.session_id
        mine = sorted({sha for sha, _ in s.named if links.get(sha) == sid},
                      key=lambda sha: (info.get(sha, ("", ""))[0], sha))
        s.commits = [(sha, info.get(sha, ("", ""))[1]) for sha in mine]
    for group in (offered, refused):
        group.sort(key=lambda s: (-parse_time(s.started_at), s.transcript.session_id))
    documents, refused_docs = _documents(store, set(config.documents.paths), declined_documents)
    try:
        head = git("rev-list", "HEAD", cwd=store.root).split()
    except GitError:
        head = []
    local, _ = _checkpoints(store, tip_paths)
    now = [sha for sha in head if sha not in local]
    days, source = claude_home.cleanup_days(store.root)
    oldest = min((s.transcript.mtime for s in offered), default=None)
    offer = Offer(
        sessions=offered, unreadable=unreadable, in_use=in_use, documents=documents,
        declined=[*refused, *refused_docs] if include_declined else [],
        unlinked_now=len(now), unlinked_after=len([sha for sha in now if sha not in links]),
        cleanup=Cleanup(days, source, None if oldest is None else _date(oldest),
                        None if oldest is None else _date(oldest + days * 86400)),
        token="", held_named=held_named)
    offer.token = hashlib.sha256("\n".join(i.key for i in offer.items).encode()).hexdigest()[:8]
    return offer


def _record(store: StorePaths, s: OfferedSession, parsed, uses: list[ToolUse]) -> SessionRecord:
    """D6: a session record from the transcript alone."""
    sid = s.transcript.session_id
    author = git_author(store)
    stamps = [e.timestamp for e in parsed.events if e.timestamp]
    record = SessionRecord(session_id=sid, author=author, branch=s.transcript.branch,
                           worktree=s.worktree, started_at=stamps[0] if stamps else None,
                           ended_at=stamps[-1] if stamps else None, imported_at=now_iso())
    record.turns = turn_records(sid, author, parsed.turns)
    totals: dict[str, int] = {}
    for t in record.turns:
        for key, value in (t.tokens or {}).items():
            totals[key] = totals.get(key, 0) + value
    record.tokens = totals or None
    files: set[str] = set()
    base = Path(s.worktree).resolve()
    for u in uses:
        command = u.input.get("command") if isinstance(u.input.get("command"), str) else None
        kind = tool_kind(u.name, command)
        record.tool_calls[kind] = record.tool_calls.get(kind, 0) + 1
        for sub, argv in ctx_invocations(command) if kind == "ctx" and command else []:
            key = ctx_call_key(sub, argv)
            record.ctx_calls[key] = record.ctx_calls.get(key, 0) + 1
        edited = u.input.get("file_path") or u.input.get("notebook_path")
        if u.name in EDIT_TOOLS and isinstance(edited, str) and edited:
            try:
                files.add((Path(s.transcript.cwd) / edited).resolve().relative_to(base).as_posix())
            except ValueError:
                pass  # outside the worktree: not recorded
    record.files_touched = sorted(files)
    record.subagents = [r.agent_id for r in parsed.subagents if is_safe_name(r.agent_id)]
    return record


def _import_session(store: StorePaths, config: Config, s: OfferedSession,
                    shas: list[str]) -> list[str]:
    """D9: copies, checkpoint records, then the session record, in one history commit.
    Returns the SHAs linked."""
    sid = s.transcript.session_id
    source = s.transcript.path
    with _session_lock(store, sid):
        existing = _read_record(store, sid)
        if read_blob(store, f"records/sessions/{sid}.json") is not None or (
                existing is not None and existing.imported_at is None):
            raise RuntimeError("already held")
        parsed = ClaudeCodeAdapter().parse(source)
        if not parsed.supported or not parsed.turns:
            raise RuntimeError("transcript unreadable")
        uses = _all_uses(source, parsed)
        record = _record(store, s, parsed, uses)
        raw = _raw_name(store, sid, record, False)
        copy_reduced(source, raw, config, store)
        config = with_account_ids(config, store)  # the copy may have taught new IDs
        pairs = copy_subagents(store, config, sid, parsed.subagents)
        config = with_account_ids(config, store)
        record.transcript_bytes = stored_sizes(raw, store.raw / "subagents" / sid) | {
            "source": source.stat().st_size + sum(p.stat().st_size for p, _ in pairs)}
        checkpoints: list[Path] = []
        lock = FileLock(store.state / "locks" / "githooks.lock", stale_after=300, timeout=120)
        with lock if shas else nullcontext():
            linked = []
            if shas:
                authors = commit_links(store, shas)
                info = _subjects(store, shas)
                for sha in shas:
                    path = checkpoint_path(store, sha)
                    if path.exists() or read_blob(store, f"records/checkpoints/{sha}.json"):
                        try:
                            mine = read_checkpoint(store, sha).session_ids == [sid]
                        except (OSError, RecordError):
                            mine = False
                        if mine:  # written by an interrupted run of this import
                            checkpoints.append(path)
                            linked.append(sha)
                        continue  # appeared in the meantime: left alone
                    files, insertions, deletions = _diff_stats(store, sha)
                    checkpoints.append(write_checkpoint(store, CheckpointRecord(
                        sha=sha, author=authors[sha][0], session_ids=[sid],
                        created_at=info[sha][0], files_changed=files, insertions=insertions,
                        deletions=deletions, rewritten_from=None), config))
                    linked.append(sha)
            path = write_session(store, record, config)
            commit_paths(store, [raw, *(c for _, c in pairs), *checkpoints, path],
                         f"wscontext: import session {sid}")
    return linked


def _item_id(item) -> str:
    return item.transcript.session_id if isinstance(item, OfferedSession) else item.path


def run(store: StorePaths, config: Config, offer: Offer, take: list, decline: list) -> Summary:
    """Import the taken items and remember the declined ones. A failed session does not stop
    the others; its error is in the summary."""
    taken, refused = {i.key for i in take}, {i.key for i in decline}
    if taken & refused:
        raise ValueError("an item cannot be both taken and declined")
    learn_account_file(store, config)
    config = with_account_ids(config, store)
    summary = Summary()
    sessions = [i for i in take if isinstance(i, OfferedSession)]
    documents = [i.path for i in take if isinstance(i, OfferedDocument)]
    ids = {s.transcript.session_id for s in sessions}
    tip = read_tip(store)
    named = {sid: n for sid, n in offer.held_named.items() if sid not in ids} | {
        s.transcript.session_id: s.named for s in sessions}
    links = _plan(store, named, ids, set(ls_blobs(store, tip)) if tip else set())
    for s in sorted(sessions, key=lambda s: (_when(s.started_at), s.transcript.session_id)):
        sid = s.transcript.session_id
        try:
            summary.commits += _import_session(
                store, config, s, sorted(sha for sha, owner in links.items() if owner == sid))
            summary.sessions.append(sid)
        except Exception as e:
            summary.errors[sid] = f"{type(e).__name__}: {e}"
            continue
        try:
            result = index_build.index_session(store, sid)
            if result.get("locked"):
                summary.errors["index"] = "the index is busy; `ctx find` catches up later"
        except Exception as e:
            summary.errors["index"] = f"{type(e).__name__}: {e}"
    if documents:
        try:
            current = list(load_config(store.root).documents.paths)
            set_document_paths(store.root, [*current, *(p for p in documents if p not in current)])
            summary.documents = documents
            index_build.index_docs(store, with_account_ids(load_config(store.root), store))
        except Exception as e:
            summary.errors["documents"] = f"{type(e).__name__}: {e}"

    def remember(d: dict) -> None:
        stamp = now_iso()
        for name, kind in (("declined_sessions", OfferedSession),
                           ("declined_documents", OfferedDocument)):
            entries = d.get(name) if isinstance(d.get(name), dict) else {}
            for item in take:
                if isinstance(item, kind):
                    entries.pop(_item_id(item), None)
            for item in decline:
                if isinstance(item, kind):
                    entries[_item_id(item)] = stamp
            if entries:
                d[name] = entries
            else:
                d.pop(name, None)

    if take or decline:
        update_json(state_path(store), remember, config)
    summary.declined = sorted(refused)
    return summary
