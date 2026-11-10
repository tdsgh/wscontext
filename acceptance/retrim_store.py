"""Re-trim this repository's own store under the current reduction list and squash its unpushed history.

    .venv/bin/python acceptance/retrim_store.py [--repo DIR] [--dry-run]
    .venv/bin/python acceptance/retrim_store.py [--repo DIR] --apply --backup DIR

A one-off for a store captured before a line type or attachment type became bookkeeping (the WP10
gate: `session-owner`, `cost-state`, `budget_usd`, `hook_success`). Each session copy and subagent
copy is re-trimmed from the stored copy itself, not from its source transcript: the lines that are
now bookkeeping go, and the keep-once fields only they carried move to the next line that needs
them, as reducing the source would have placed them. Every other line keeps its bytes. A copy is
rewritten only when the result parses the same, is in the current stored shape, and differs from
the stored copy in nothing else. PreCompact snapshots are never rewritten; the ones holding an
account identifier key are reported.

The history ref is then replaced by one root commit holding its tip tree after the re-trim, with
compare-and-swap, so no blob of an earlier commit stays reachable from it. Records, checkpoints and
commit links stay. The search index is rebuilt afterwards.

The dry run (the default) prints the plan and writes nothing. `--apply` needs `--backup DIR`
outside the repository: a bundle of the history ref and a copy of `raw/`, `records/` and
`state/materialized.json` go there first. Both refuse, and `--apply` writes nothing, when any
history may have been pushed (a Git remote, `[sync] remote`, a tracking ref, the stage-1 branch, or
another ref holding history commits), when a session still active in `state/active.json` needs a
change, or when a copy fails its check. Unreachable objects stay in the object database until Git
prunes them; nothing here runs `git gc`. Not part of the offline suite (pytest never collects
`acceptance/`).
"""

import argparse
import contextlib
import dataclasses
import difflib
import json
import shlex
import shutil
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from record_corpus import ACCOUNT_KEYS, RAW_NAME, parsed_view
from wscontext.adapters.claude_code import KEEP_ONCE, REDUCED_KINDS, entry_kind, reduce
from wscontext.capture.state import active_path, last_activity, read_json, update_json
from wscontext.capture.worker import SWEEP_STALE_SECONDS, stored_sizes
from wscontext.cli.data import find_session, transcript_turns
from wscontext.config import Config, load_config
from wscontext.gitutil import GitError, git, git_bytes
from wscontext.history.branch import (HISTORY_REF, LEGACY_REF, commit_paths, ls_blobs, read_tip,
                                      squash_unshared)
from wscontext.history.sync import shared_tips
from wscontext.index import build as index_build
from wscontext.redact.redactor import redact_bytes
from wscontext.store.locks import FileLock, LockTimeout
from wscontext.store.paths import StorePaths, is_safe_name
from wscontext.store.records import RecordError, SessionRecord, atomic_write, read_session, write_session

MESSAGE = "wscontext: re-trim stored copies"
LOCK_TIMEOUT = 10
MISSING = object()


# --- the re-trim of one stored copy ------------------------------------------------------------

def _obj(line: bytes) -> dict | None:
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def label(obj: dict) -> str:
    """A line's type as the report names it: `session-owner`, `attachment:hook_success`."""
    attachment = obj.get("attachment")
    if obj.get("type") == "attachment" and isinstance(attachment, dict):
        return f"attachment:{attachment.get('type')}"
    return str(obj.get("type"))


def _has_key(value) -> bool:
    if isinstance(value, dict):
        return any(k in ACCOUNT_KEYS or _has_key(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_has_key(v) for v in value)
    return False


def account_lines(data: bytes) -> Counter:
    """Lines holding an account identifier key, by type: at the top level of any line, or anywhere
    in a bookkeeping line. Text that only mentions an identifier, a prompt or a tool result, is not
    a key and does not count."""
    found = Counter()
    for line in data.splitlines():
        obj = _obj(line)
        if obj and (any(k in obj for k in ACCOUNT_KEYS)
                    or (entry_kind(obj, sidechain=True) == "bookkeeping" and _has_key(obj))):
            found[label(obj)] += 1
    return found


def rereduce(data: bytes) -> tuple[bytes, Counter, int]:
    """A stored copy under the current reduction: (new bytes, dropped lines by type, lines given
    keep-once fields).

    Plain `reduce` of a stored copy would lose a keep-once field whose only carrier is a dropped
    line, `version` among them, so the copy would no longer parse. Here such a field goes onto the
    next reduced line whose last written value differs, where reducing the source would have kept
    it; a field equal to the last written value goes, as `reduce` drops it."""
    out: list[bytes] = []
    dropped: Counter = Counter()
    carried = 0
    seen: dict = {}  # keep-once field -> its value as of this line, dropped lines included
    written: dict = {}  # keep-once field -> the value last written to the output
    for line in data.splitlines(keepends=True):
        body = line.rstrip(b"\r\n")
        obj = _obj(body)
        if obj is None:
            out.append(line)
            continue
        seen.update((k, obj[k]) for k in KEEP_ONCE if k in obj)
        kind = entry_kind(obj, sidechain=True)
        if kind == "bookkeeping":
            dropped[label(obj)] += 1
            continue
        if kind in REDUCED_KINDS:
            changed = False
            for k in KEEP_ONCE:
                if k not in obj and k in seen and written.get(k, MISSING) != seen[k]:
                    obj[k] = seen[k]
                    changed = True
                elif k in obj and written.get(k, MISSING) == obj[k]:
                    del obj[k]
                    changed = True
            if changed:
                carried += 1
                line = json.dumps(obj, ensure_ascii=False,
                                  separators=(",", ":")).encode() + line[len(body):]
        written.update((k, obj[k]) for k in KEEP_ONCE if k in obj)
        out.append(line)
    return b"".join(out), dropped, carried


def _effective(objs: list[dict | None]) -> list[dict]:
    """The keep-once values a reader sees at each line: the last one written so far."""
    state: dict = {}
    out = []
    for obj in objs:
        if obj:
            state.update((k, obj[k]) for k in KEEP_ONCE if k in obj)
        out.append(dict(state))
    return out


def _view(data: bytes, sidechain: bool) -> tuple:
    with tempfile.TemporaryDirectory(prefix="wscontext-retrim-") as tmp:
        path = Path(tmp) / "copy.jsonl"
        path.write_bytes(data)
        return parsed_view(path, sidechain)


def check_retrim(old: bytes, new: bytes, cut: int, sidechain: bool) -> list[str]:
    """What the re-trim changed beyond dropping bookkeeping lines and moving keep-once fields, by a
    line diff of the two copies; empty when nothing."""
    problems = []
    if reduce(new, cut) != new:
        problems.append("not in the current stored shape: the reduction would change more than "
                        "the newly named kinds")
    a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
    objs_a, objs_b = [_obj(x) for x in a], [_obj(x) for x in b]
    pairs = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            pairs += zip(range(i1, i2), range(j1, j2))
            continue
        kept = [i for i in range(i1, i2)
                if not (objs_a[i] and entry_kind(objs_a[i], sidechain=True) == "bookkeeping")]
        if len(kept) != j2 - j1:
            problems.append(f"lines {i1 + 1}-{i2}: {j2 - j1} line(s) where {len(kept)} are kept")
            continue
        for i, j in zip(kept, range(j1, j2)):
            x, y = objs_a[i], objs_b[j]
            if x is None or y is None or ({k: v for k, v in x.items() if k not in KEEP_ONCE}
                                          != {k: v for k, v in y.items() if k not in KEEP_ONCE}):
                problems.append(f"line {i + 1}: changed beyond its keep-once fields")
            else:
                pairs.append((i, j))
    ea, eb = _effective(objs_a), _effective(objs_b)
    lost = sorted({k for i, j in pairs
                   if objs_a[i] and entry_kind(objs_a[i], sidechain=True) in REDUCED_KINDS
                   for k in KEEP_ONCE if ea[i].get(k, MISSING) != eb[j].get(k, MISSING)})
    if lost:
        problems.append(f"keep-once field(s) {', '.join(lost)} read differently")
    if _view(old, sidechain) != _view(new, sidechain):
        problems.append("parses differently")
    return problems


# --- the plan ------------------------------------------------------------------------------------

@dataclass
class Copy:
    path: Path
    rel: str
    sid: str
    sidechain: bool
    old: bytes
    new: bytes
    dropped: Counter
    carried: int
    redactions: int
    problems: list[str]
    tip: str | None  # its blob on the history tip, when there is one


@dataclass
class Plan:
    tip: str | None
    commits: list[str] = field(default_factory=list)  # "<sha> <subject>", newest first
    reflog: bool = False
    copies: list[Copy] = field(default_factory=list)
    stale_tip: list[str] = field(default_factory=list)  # tip blobs to replace by the current local copy
    records: dict[str, tuple[SessionRecord, int, int]] = field(default_factory=dict)  # sid -> new, stored before, after
    snapshots: list[tuple[str, Counter, bool]] = field(default_factory=list)  # rel, account lines, on the tip
    dirty_blobs: list[tuple[str, str, int, int]] = field(default_factory=list)  # sha, path, bookkeeping, account lines
    blockers: list[str] = field(default_factory=list)

    @property
    def sessions(self) -> set[str]:
        return {c.sid for c in self.copies} | set(self.records) | {
            _owner(rel)[0] for rel in self.stale_tip if _owner(rel)}

    @property
    def squash(self) -> bool:
        return self.tip is not None and bool(self.dirty_blobs)


def _owner(rel: str) -> tuple[str, bool, bool] | None:
    """(session ID, is a subagent copy, is a PreCompact snapshot) of a `raw/` path."""
    parts = rel.split("/")
    if len(parts) == 3 and parts[:2] == ["raw", "sessions"] and (m := RAW_NAME.fullmatch(parts[2])):
        return (m["sid"], False, bool(m["pc"])) if is_safe_name(m["sid"]) else None
    if len(parts) == 4 and parts[:2] == ["raw", "subagents"] and parts[3].endswith(".jsonl") \
            and is_safe_name(parts[2]) and is_safe_name(parts[3]):
        return parts[2], True, False
    return None


def _blobs(store: StorePaths, shas: list[str]) -> dict[str, bytes]:
    """The content of each blob, read in one `cat-file --batch`."""
    if not shas:
        return {}
    out = git_bytes("cat-file", "--batch", cwd=store.root, input="".join(f"{s}\n" for s in shas).encode())
    found, pos = {}, 0
    for sha in shas:
        end = out.index(b"\n", pos)
        size = int(out[pos:end].split()[2])
        found[sha] = out[end + 1:end + 1 + size]
        pos = end + 2 + size
    return found


def reachable_blobs(store: StorePaths) -> list[tuple[str, str]]:
    """(sha, path) of every blob reachable from the history ref, each once."""
    if read_tip(store) is None:
        return []
    objects = [line.split(" ", 1) for line in git("rev-list", "--objects", HISTORY_REF,
                                                   cwd=store.root).splitlines() if " " in line]
    kinds = git("cat-file", "--batch-check=%(objecttype)", cwd=store.root,
                input="".join(f"{sha}\n" for sha, _ in objects)).split()
    return [(sha, path) for (sha, path), kind in zip(objects, kinds) if kind == "blob"]


def _bookkeeping_lines(data: bytes) -> int:
    return sum(1 for line in data.splitlines()
               if (obj := _obj(line)) and entry_kind(obj, sidechain=True) == "bookkeeping")


def pushed_refs(store: StorePaths, config: Config) -> list[str]:
    """Why any history may already be on a remote; empty when all of it is unpushed."""
    found = [f"Git remote {r!r} is configured" for r in git("remote", cwd=store.root).split()]
    if config.sync.remote:
        found.append(f"[sync] remote is set to {config.sync.remote!r}")
    refs = git("for-each-ref", "--format=%(refname)", "refs/wscontext/", LEGACY_REF, "refs/remotes/",
               cwd=store.root).split()
    found += [f"ref {r} exists" for r in refs if r != HISTORY_REF
              and (r.startswith(("refs/wscontext/", LEGACY_REF)) or "/wscontext/" in r)]
    if read_tip(store):
        roots = git("rev-list", "--max-parents=0", HISTORY_REF, cwd=store.root).split()
        holders = {r for root in roots for r in git("for-each-ref", "--format=%(refname)",
                                                     "--contains", root, cwd=store.root).split()}
        found += [f"ref {r} holds history commits" for r in sorted(holders - {HISTORY_REF})]
    return found


def make_plan(store: StorePaths, config: Config) -> Plan:
    """What the re-trim and the squash would do; reads only."""
    tip = read_tip(store)
    plan = Plan(tip)
    plan.blockers += pushed_refs(store, config)
    if tip:
        plan.commits = git("log", "--format=%H %s", HISTORY_REF, cwd=store.root).splitlines()
        try:
            git("reflog", "exists", HISTORY_REF, cwd=store.root)
            plan.reflog = True
        except GitError:
            pass
    on_tip = {path: sha for path, (_, sha, _) in ls_blobs(store, tip).items()} if tip else {}
    cut = config.capture.tool_result_max_chars

    local = sorted(p for p in store.raw.rglob("*.jsonl") if p.is_file()) if store.raw.is_dir() else []
    for path in local:
        rel = path.relative_to(store.local).as_posix()
        who = _owner(rel)
        if who is None:
            continue
        sid, sidechain, snapshot = who
        data = path.read_bytes()
        if snapshot:
            if keys := account_lines(data):
                plan.snapshots.append((rel, keys, rel in on_tip))
            continue
        trimmed, dropped, carried = rereduce(data)
        if trimmed == data:
            continue
        problems = check_retrim(data, trimmed, cut, sidechain)
        new, hits = redact_bytes(trimmed, config)
        plan.copies.append(Copy(path, rel, sid, sidechain, data, new, dropped, carried, len(hits),
                                problems, on_tip.get(rel)))

    # tip blobs: each needs the re-trim done locally, or a local copy that already has it
    copies = {c.rel: c for c in plan.copies}
    tip_raw = {rel: sha for rel, sha in on_tip.items() if _owner(rel)}
    contents = _blobs(store, sorted(set(tip_raw.values())))
    for rel, sha in sorted(tip_raw.items()):
        sid, _, snapshot = _owner(rel)
        blob = contents[sha]
        if snapshot:
            if account_lines(blob) and not any(s[0] == rel for s in plan.snapshots):
                plan.snapshots.append((rel, account_lines(blob), True))
            continue
        target = store.local.joinpath(*rel.split("/"))
        current = copies[rel].new if rel in copies else (target.read_bytes() if target.is_file() else None)
        if current is not None and current == blob:
            continue
        trimmed = rereduce(blob)[0]
        if trimmed == blob and rel not in copies:
            continue  # a clean tip blob behind a newer local copy: the session's next commit sends it
        if current is None:
            plan.blockers.append(f"{rel}: the history tip holds a copy to re-trim and there is no "
                                 "local copy (pruned?)")
        elif not current.startswith(redact_bytes(trimmed, config)[0]):
            plan.blockers.append(f"{rel}: the history tip and the local copy diverge; the local "
                                 "copy does not continue the re-trimmed tip blob")
        elif rel not in copies:
            plan.stale_tip.append(rel)

    reachable = [(sha, path) for sha, path in reachable_blobs(store) if _owner(path)]
    contents = _blobs(store, sorted({sha for sha, _ in reachable}))
    for sha, path in reachable:
        book = 0 if _owner(path)[2] else _bookkeeping_lines(contents[sha])
        keys = sum(account_lines(contents[sha]).values())
        if book or keys:
            plan.dirty_blobs.append((sha, path, book, keys))

    for c in plan.copies:
        if c.problems:
            plan.blockers.append(f"{c.rel}: the re-trim fails its check: {'; '.join(c.problems)}")
    for snap, _, tipped in plan.snapshots:
        if tipped:
            plan.blockers.append(f"{snap}: a PreCompact snapshot on the history tip holds account "
                                 "keys; this tool never rewrites snapshots")
    _plan_records(store, plan)
    _check_active(store, config, plan)
    return plan


def _plan_records(store: StorePaths, plan: Plan) -> None:
    """Each changed session's record with `transcript_bytes` measured on the re-trimmed copies."""
    changed = {c.path: c.new for c in plan.copies}
    for sid in sorted({c.sid for c in plan.copies}):
        try:
            rec = read_session(store, sid)
        except (OSError, RecordError):
            continue
        mains = sorted((store.raw / "sessions").glob(f"????-??-??-{sid}.jsonl"))
        if not rec.transcript_bytes or not mains:
            continue
        with tempfile.TemporaryDirectory(prefix="wscontext-retrim-") as tmp:
            main = Path(tmp) / "main.jsonl"
            main.write_bytes(changed.get(mains[0]) or mains[0].read_bytes())
            subs = Path(tmp) / "subagents"
            subs.mkdir()
            for p in (store.raw / "subagents" / sid).glob("*.jsonl"):
                (subs / p.name).write_bytes(changed.get(p) or p.read_bytes())
            sizes = stored_sizes(main, subs)
        if "source" in rec.transcript_bytes:
            sizes["source"] = rec.transcript_bytes["source"]
        if sizes != rec.transcript_bytes:
            plan.records[sid] = (dataclasses.replace(rec, transcript_bytes=sizes),
                                 rec.transcript_bytes.get("stored", 0), sizes["stored"])


def _check_active(store: StorePaths, config: Config, plan: Plan) -> None:
    cutoff = time.time() - config.capture.stale_session_minutes * 60
    for sessions in read_json(active_path(store)).values():
        for sid, started in (sessions.items() if isinstance(sessions, dict) else ()):
            if sid not in plan.sessions:
                continue
            last = last_activity(store, sid, started)
            idle = (f", idle since {time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(last))}, "
                    "past the staleness window: finalize it with the sweep first") if last < cutoff else ""
            plan.blockers.append(f"session {sid} is active in state/active.json and needs a change{idle}")


def sweep_command(store: StorePaths) -> str:
    return shlex.join([".venv/bin/python", "-m", "wscontext.capture.worker", "sweep", "", "",
                       str(store.root)])


# --- the report ----------------------------------------------------------------------------------

def _kinds(counts: Counter) -> str:
    return ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))


def render(store: StorePaths, plan: Plan) -> str:
    out = [f"store: {store.local}"]
    if plan.tip:
        out.append(f"history: {HISTORY_REF} at {plan.tip[:12]}, {len(plan.commits)} commit(s), "
                   f"reflog: {'yes' if plan.reflog else 'none'}")
    else:
        out.append(f"history: no {HISTORY_REF}")
    out += ["", f"copies to re-trim: {len(plan.copies)}"]
    for c in plan.copies:
        before, after = sum(account_lines(c.old).values()), sum(account_lines(c.new).values())
        out.append(f"  {c.rel}{'  [on the tip]' if c.tip else '  [local only]'}")
        out.append(f"    drops {_kinds(c.dropped)}")
        if c.carried:
            out.append(f"    keep-once fields moved onto {c.carried} line(s)")
        out.append(f"    {len(c.old):,} -> {len(c.new):,} bytes; account-key lines {before} -> {after}"
                   + (f"; {c.redactions} redaction(s)" if c.redactions else ""))
        out += [f"    CHECK FAILED: {p}" for p in c.problems]
    if plan.stale_tip:
        out += ["", "tip blobs replaced by the current local copy:", *(f"  {r}" for r in plan.stale_tip)]
    if plan.records:
        out += ["", "records: transcript_bytes.stored"]
        out += [f"  records/sessions/{sid}.json  {a:,} -> {b:,}" for sid, (_, a, b) in plan.records.items()]
    if plan.snapshots:
        out += ["", "PreCompact snapshots holding account keys (never rewritten):"]
        out += [f"  {rel}: {_kinds(keys)}; {'on the history tip' if tip else 'local only, never on the history ref'}"
                for rel, keys, tip in plan.snapshots]
    if plan.squash:
        out += ["", f"history rewrite: these {len(plan.commits)} commit(s) are replaced by one root "
                    "commit holding the tip tree after the re-trim:"]
        out += [f"  {sha[:12]} {subject}" for sha, _, subject in (c.partition(" ") for c in plan.commits)]
        out.append(f"  reachable blobs with bookkeeping or account-key lines: {len(plan.dirty_blobs)}")
        out += [f"    {sha[:12]} {path}: {book} bookkeeping, {keys} account-key line(s)"
                for sha, path, book, keys in plan.dirty_blobs]
    elif plan.tip:
        out += ["", "history rewrite: none needed"]
    if plan.blockers:
        out += ["", "apply refuses:", *(f"  - {b}" for b in plan.blockers)]
        if any("sweep" in b for b in plan.blockers):
            out.append(f"  finalize idle sessions with: {sweep_command(store)}")
    return "\n".join(out)


# --- apply ---------------------------------------------------------------------------------------

@contextlib.contextmanager
def store_locks(store: StorePaths, sessions: set[str]):
    """The sweep's, each session worker's and the Git hooks' locks, in the order those take them."""
    locks = store.state / "locks"
    with contextlib.ExitStack() as stack:
        stack.enter_context(FileLock(locks / "sweep.lock", stale_after=SWEEP_STALE_SECONDS,
                                     timeout=LOCK_TIMEOUT))
        for sid in sorted(sessions):
            stack.enter_context(FileLock(locks / f"worker-{sid}.lock", stale_after=300,
                                         timeout=LOCK_TIMEOUT))
        stack.enter_context(FileLock(locks / "githooks.lock", stale_after=300, timeout=LOCK_TIMEOUT))
        yield


def write_backup(store: StorePaths, backup: Path) -> list[str]:
    backup.mkdir(parents=True, exist_ok=True)
    written = []
    if read_tip(store):
        bundle = backup / "history.bundle"
        git("bundle", "create", str(bundle), HISTORY_REF, cwd=store.root)
        git("bundle", "verify", str(bundle), cwd=store.root)
        written.append(str(bundle))
    for name in ("raw", "records"):
        if (store.local / name).is_dir():
            shutil.copytree(store.local / name, backup / "store" / name)
            written.append(str(backup / "store" / name))
    seen = store.state / "materialized.json"
    if seen.is_file():
        (backup / "store" / "state").mkdir(parents=True, exist_ok=True)
        shutil.copy2(seen, backup / "store" / "state" / seen.name)
        written.append(str(backup / "store" / "state" / seen.name))
    return written


def apply_plan(store: StorePaths, config: Config, plan: Plan) -> str | None:
    """Rewrite the local copies and records, commit them where the tip holds them, then squash.
    Returns the new tip."""
    for c in plan.copies:
        atomic_write(c.path, c.new)
    for rec, _, _ in plan.records.values():
        write_session(store, rec, config)
    tip = read_tip(store)
    if tip is None:
        return None
    on_tip = ls_blobs(store, tip)
    rels = [c.rel for c in plan.copies if c.rel in on_tip] + plan.stale_tip
    rels += [f"records/sessions/{sid}.json" for sid in plan.records
             if f"records/sessions/{sid}.json" in on_tip]
    if rels:
        commit_paths(store, [store.local.joinpath(*r.split("/")) for r in rels], MESSAGE)
    new = squash_unshared(store, shared_tips(store), MESSAGE) if plan.squash else read_tip(store)
    seen = store.state / "materialized.json"
    if seen.is_file() and new:
        blobs = {path: sha for path, (_, sha, _) in ls_blobs(store, new).items()}
        update_json(seen, lambda d: d.update({r: blobs[r] for r in rels if r in blobs}), config)
    return new


@dataclass
class Before:
    prompts: dict[str, list]
    checkpoints: dict[str, bytes]


def _checkpoints(store: StorePaths) -> dict[str, bytes]:
    """Every checkpoint record, local and on the tip, by where it is."""
    found = {f"local:{p.name}": p.read_bytes()
             for p in sorted((store.records / "checkpoints").glob("*.json"))}
    tip = read_tip(store)
    if tip:
        on_tip = {p: sha for p, (_, sha, _) in ls_blobs(store, tip).items()
                  if p.startswith("records/checkpoints/")}
        contents = _blobs(store, sorted(set(on_tip.values())))
        found |= {f"tip:{p}": contents[sha] for p, sha in on_tip.items()}
    return found


def _prompts(store: StorePaths, sessions: set[str]) -> dict[str, list]:
    out = {}
    for sid in sorted(sessions):
        rec = find_session(store, sid)
        out[sid] = None if not isinstance(rec, SessionRecord) else [
            (t.index, t.prompt) for t in transcript_turns(store, rec)]
    return out


def verify(store: StorePaths, plan: Plan, before: Before) -> list[str]:
    """What the applied re-trim left wrong; empty when nothing."""
    problems = []
    for path in sorted(store.raw.rglob("*.jsonl")):
        rel = path.relative_to(store.local).as_posix()
        who = _owner(rel)
        if who and not who[2]:
            data = path.read_bytes()
            if account_lines(data) or _bookkeeping_lines(data):
                problems.append(f"{rel}: still holds bookkeeping or account-key lines")
    reachable = reachable_blobs(store)
    contents = _blobs(store, sorted({sha for sha, _ in reachable}))
    for sha, path in reachable:
        data, who = contents[sha], _owner(path)
        if account_lines(data) or (who and not who[2] and _bookkeeping_lines(data)):
            problems.append(f"blob {sha[:12]} {path} reachable from {HISTORY_REF} still holds "
                            "bookkeeping or account-key lines")
    for sid, (rec, _, _) in plan.records.items():
        main = sorted((store.raw / "sessions").glob(f"????-??-??-{sid}.jsonl"))[0]
        sizes = stored_sizes(main, store.raw / "subagents" / sid)
        if {k: v for k, v in read_session(store, sid).transcript_bytes.items() if k != "source"} != sizes:
            problems.append(f"records/sessions/{sid}.json: transcript_bytes do not match the copies")
    after = _prompts(store, set(before.prompts))
    problems += [f"session {sid} no longer resolves to the same turns"
                 for sid in before.prompts if after[sid] != before.prompts[sid]]
    if _checkpoints(store) != before.checkpoints:
        problems.append("checkpoint records changed")
    return problems


def _outside(path: Path, store: StorePaths) -> bool:
    return not any(path.is_relative_to(p.resolve()) for p in (store.root, store.local.parent))


def run_apply(store: StorePaths, config: Config, backup: Path) -> int:
    backup = backup.expanduser().resolve()
    if not _outside(backup, store):
        print(f"--backup {backup} is inside the repository; give a directory outside it", file=sys.stderr)
        return 2
    if backup.exists() and (not backup.is_dir() or any(backup.iterdir())):
        print(f"--backup {backup} exists and is not an empty directory", file=sys.stderr)
        return 2
    plan = make_plan(store, config)
    try:
        with store_locks(store, plan.sessions):
            locked = plan.sessions
            plan = make_plan(store, config)  # again, now that no worker can write
            print(render(store, plan))
            if plan.blockers:
                print("\nnothing written", file=sys.stderr)
                return 1
            if not plan.sessions <= locked:
                print("\nthe store changed while planning; nothing written, run again", file=sys.stderr)
                return 1
            if not (plan.copies or plan.stale_tip or plan.records or plan.squash):
                print("\nnothing to do")
                return 0
            before = Before(_prompts(store, plan.sessions), _checkpoints(store))
            saved = write_backup(store, backup)
            new = apply_plan(store, config, plan)
    except LockTimeout as e:
        print(f"a worker or Git hook holds {e}; nothing written, try again shortly", file=sys.stderr)
        return 1
    print("\nbackup:", *(f"  {p}" for p in saved), sep="\n")
    print(f"restore: git fetch {shlex.quote(str(backup / 'history.bundle'))} "
          f"'+{HISTORY_REF}:{HISTORY_REF}' and copy {backup / 'store'}/* back into {store.local}")
    if new:
        count = git("rev-list", "--count", HISTORY_REF, cwd=store.root).strip()
        print(f"\nhistory: {HISTORY_REF} at {new[:12]}, {count} commit(s)")
    result = index_build.rebuild(store)
    print("index: " + ("locked by another process; run ctx rebuild" if result.get("locked")
                       else f"rebuilt, {result['sessions_indexed']} sessions, "
                            f"{result['commits_indexed']} commits"))
    problems = verify(store, plan, before)
    print("\nverify: " + ("ok" if not problems else "FAILED"), *(f"  - {p}" for p in problems), sep="\n")
    print("\nThe replaced commits and blobs stay in the object database until Git prunes them; "
          "nothing here runs git gc.")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="the repository (default: here)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the plan, write nothing (default)")
    mode.add_argument("--apply", action="store_true", help="re-trim, back up first, squash the history")
    parser.add_argument("--backup", type=Path, metavar="DIR",
                        help="with --apply: an empty or new directory outside the repository")
    args = parser.parse_args(argv)
    if args.apply and args.backup is None:
        parser.error("--apply needs --backup DIR")
    store = StorePaths.resolve(args.repo)
    config = load_config(store.root)
    if args.apply:
        return run_apply(store, config, args.backup)
    plan = make_plan(store, config)
    print(render(store, plan))
    print("\ndry run: nothing written")
    return 1 if plan.blockers else 0


if __name__ == "__main__":
    sys.exit(main())
