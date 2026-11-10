"""Refresh the graph from the working tree.

The files are the Git-listed paths (tracked and untracked, not ignored) with an extension of a
switched-on language, minus `[graph] exclude`. Each row keeps size, mtime, content digest and its
language part's version: a refresh reparses only files whose content or part changed, so running
it again changes nothing.
"""

import hashlib
import logging
import os
import sqlite3
import stat
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from wscontext.config import Config
from wscontext.gitutil import git_bytes
from wscontext.graph import db, history, parse
from wscontext.graph.languages import REGISTRY, load_parts
from wscontext.graph.languages.base import FileFacts, Import, LanguagePart, RepoFiles
from wscontext.graph.scope import MAX_FILE_BYTES, excluded
from wscontext.store.locks import FileLock, LockTimeout
from wscontext.store.paths import StorePaths

log = logging.getLogger(__name__)

LOCK_STALE_SECONDS = 600
REBUILD_LOCK_TIMEOUT = 60
# files written per transaction, so readers and a budget stop see progress
BATCH = 200
# an mtime this close to the start of a refresh can hide a same-size edit made in the same tick;
# such a row stores mtime 0, so the next refresh compares its digest again
RACY_NS = 2_000_000_000
WORKER = "wscontext.graph.worker"
# a worker clears its claim as it tries the lock; a claim this old is from one that never started
CLAIM_STALE_SECONDS = 60


@dataclass(frozen=True)
class Freshness:
    # every listed file is current
    complete: bool
    # another writer held the lock: the graph is as that writer leaves it
    busy: bool = False
    # files found changed but left for a later refresh when the budget ran out
    pending_files: int = 0
    # paths whose last parse found a syntax error
    parse_errors: tuple[str, ...] = ()
    # switched-on languages whose part did not load
    unavailable: tuple[str, ...] = ()
    # registered languages that are switched off
    off: tuple[str, ...] = ()
    # files parsed by this refresh
    parsed: int = 0
    # commits whose co-change or session links are left for a later refresh
    pending_history: int = 0


def _listed(store: StorePaths, exclude: Sequence[str]) -> list[str]:
    """Tracked and untracked files present in the working tree, not ignored or excluded."""
    listed = set(git_bytes("ls-files", "-co", "--exclude-standard", "-z", cwd=store.root)
                 .split(b"\0"))
    listed -= set(git_bytes("ls-files", "-d", "-z", cwd=store.root).split(b"\0"))
    paths = set()
    for raw in listed:
        try:
            path = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if path and not excluded(path, exclude):
            paths.add(path)
    return sorted(paths)


def _extension(path: str) -> str:
    return PurePosixPath(path).suffix.lower()


def _owner(found: list[tuple[int, int, int]], line: int) -> int | None:
    """The id of the definition, among those sharing a qualname, whose lines hold `line`."""
    for id_, start, end in found:
        if start <= line <= end:
            return id_
    return found[-1][0] if found else None


def _insert_imports(conn: sqlite3.Connection, path: str, part: LanguagePart, imports,
                    files: RepoFiles) -> None:
    rows = []
    for imp in imports:
        try:
            targets = part.resolve_import(imp, path, files)
        except Exception as e:
            log.warning("graph: %s: resolving %r failed: %s", path, imp.spec, e)
            targets = []
        names = ",".join(imp.names)
        rows += [(path, imp.spec, names, t, imp.line) for t in targets] or [
            (path, imp.spec, names, None, imp.line)]
    conn.executemany("INSERT INTO imports(path, spec, names, target, line) VALUES(?,?,?,?,?)",
                     rows)


def _drop_facts(conn: sqlite3.Connection, path: str) -> None:
    for table in ("symbols", "imports", "calls"):
        conn.execute(f"DELETE FROM {table} WHERE path=?", (path,))


def _put_facts(conn: sqlite3.Connection, path: str, part: LanguagePart, facts: FileFacts,
               files: RepoFiles) -> None:
    _drop_facts(conn, path)
    by_qualname: dict[str, list[tuple[int, int, int]]] = {}
    for d in facts.definitions:
        parent = _owner(by_qualname.get(d.parent, []), d.start) if d.parent else None
        cur = conn.execute(
            "INSERT INTO symbols(path, name, qualname, kind, start_line, end_line, signature, "
            "parent_id) VALUES(?,?,?,?,?,?,?,?)",
            (path, d.name, d.qualname, d.kind, d.start, d.end, d.signature, parent))
        by_qualname.setdefault(d.qualname, []).append((cur.lastrowid, d.start, d.end))
    _insert_imports(conn, path, part, facts.imports, files)
    conn.executemany(
        "INSERT INTO calls(path, caller_id, name, line) VALUES(?,?,?,?)",
        [(path, _owner(by_qualname.get(c.caller, []), c.line) if c.caller else None, c.name,
          c.line) for c in facts.calls])


def _put_file(conn: sqlite3.Connection, path: str, part: LanguagePart, size: int, mtime_ns: int,
              digest: str, status: str, error: str | None) -> None:
    conn.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?)",
                 (path, part.name, part.version, size, mtime_ns, digest, status, error))


def _resolve_all(conn: sqlite3.Connection, parts: dict[str, LanguagePart],
                 files: RepoFiles) -> None:
    """Resolve every stored import again: the set of listed files changed."""
    rows = conn.execute("SELECT DISTINCT i.path, f.language, i.spec, i.names, i.line "
                        "FROM imports i JOIN files f ON f.path = i.path").fetchall()
    conn.execute("DELETE FROM imports")
    for path, language, spec, names, line in rows:
        part = parts.get(language)
        if part is not None:
            imp = Import(spec=spec, names=tuple(n for n in names.split(",") if n), line=line)
            _insert_imports(conn, path, part, [imp], files)


def _set_meta(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    conn.executemany("INSERT OR REPLACE INTO meta VALUES(?, ?)", list(values.items()))


def _record_timing(conn: sqlite3.Connection, started: float, parsed: int, changed: bool,
                   starts_build: bool, incomplete: bool) -> None:
    """Keep the last refresh that changed files and the last full build in `meta`. A build from
    an empty graph can span a budgeted refresh and the worker that finishes it: its time and
    parsed count add up in `build_pending_*` until a refresh completes it."""
    meta = dict(conn.execute("SELECT key, value FROM meta"))
    building = "build_pending_ms" in meta
    if not changed and not building:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ms = int((time.monotonic() - started) * 1000)
    values = {}
    if changed:
        values.update(last_refresh_at=now, last_refresh_ms=str(ms),
                      last_refresh_parsed=str(parsed))
    if starts_build or building:
        total_ms = int(meta.get("build_pending_ms", "0")) + ms
        total_parsed = int(meta.get("build_pending_parsed", "0")) + parsed
        if incomplete:
            values.update(build_pending_ms=str(total_ms), build_pending_parsed=str(total_parsed))
        else:
            values.update(last_build_at=now, last_build_ms=str(total_ms),
                          last_build_parsed=str(total_parsed))
    conn.execute("BEGIN IMMEDIATE")
    try:
        _set_meta(conn, values)
        if (starts_build or building) and not incomplete:
            conn.execute("DELETE FROM meta WHERE key IN ('build_pending_ms', "
                         "'build_pending_parsed')")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _walk(store: StorePaths, config: Config, parts: list[LanguagePart],
          budget_ms: int | None) -> tuple[int, int, tuple[str, ...], int]:
    """One refresh under the lock. Returns (parsed, pending files, parse errors, pending
    history)."""
    started = time.monotonic()
    started_ns = time.time_ns()
    deadline = None if budget_ms is None else started + budget_ms / 1000
    by_name = {p.name: p for p in parts}
    by_ext = {ext: p for p in parts for ext in p.extensions}
    listed = _listed(store, config.graph.exclude)
    files = RepoFiles(listed)
    wanted = {p: by_ext[_extension(p)] for p in listed if _extension(p) in by_ext}
    listed_digest = hashlib.sha256("\0".join(listed).encode("utf-8")).hexdigest()

    conn = db.connect(store)
    try:
        rows = {r[0]: r[1:] for r in conn.execute(
            "SELECT path, language, lang_version, size, mtime_ns, digest FROM files")}
        was_empty = not rows
        gone = [p for p, r in rows.items() if p not in wanted or wanted[p].name != r[0]]
        todo: list[tuple[str, LanguagePart, os.stat_result]] = []
        for path, part in wanted.items():
            try:
                st = os.stat(store.root.joinpath(*path.split("/")))
            except OSError:
                st = None
            if st is None or not stat.S_ISREG(st.st_mode):
                if path in rows:
                    gone.append(path)
                continue
            row = rows.get(path)
            if row and row[:4] == (part.name, part.version, st.st_size, st.st_mtime_ns):
                continue
            todo.append((path, part, st))

        changed = bool(gone)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for path in gone:
                _drop_facts(conn, path)
                conn.execute("DELETE FROM files WHERE path=?", (path,))
            meta = dict(conn.execute("SELECT key, value FROM meta"))
            if meta.get("listed_digest") != listed_digest:
                _resolve_all(conn, by_name, files)
                _set_meta(conn, {"listed_digest": listed_digest})
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

        parsed = pending = 0
        conn.execute("BEGIN IMMEDIATE")
        try:
            for i, (path, part, st) in enumerate(todo):
                if parsed and deadline is not None and time.monotonic() >= deadline:
                    pending = len(todo) - i
                    break
                if i and i % BATCH == 0:
                    conn.execute("COMMIT")
                    conn.execute("BEGIN IMMEDIATE")
                mtime = st.st_mtime_ns if started_ns - st.st_mtime_ns > RACY_NS else 0
                if st.st_size > MAX_FILE_BYTES:
                    _drop_facts(conn, path)
                    _put_file(conn, path, part, st.st_size, mtime, "", "skipped",
                              f"over {MAX_FILE_BYTES} bytes")
                    changed = True
                    continue
                try:
                    data = store.root.joinpath(*path.split("/")).read_bytes()
                except OSError:
                    _drop_facts(conn, path)
                    conn.execute("DELETE FROM files WHERE path=?", (path,))
                    changed = True
                    continue
                digest = hashlib.sha256(data).hexdigest()
                row = rows.get(path)
                if row and row[:2] == (part.name, part.version) and row[4] == digest:
                    if row[3] != mtime:
                        conn.execute("UPDATE files SET size=?, mtime_ns=? WHERE path=?",
                                     (st.st_size, mtime, path))
                        changed = True
                    continue
                try:
                    facts = parse.facts(part, data, path)
                except Exception as e:  # one bad file never blocks the rest
                    log.warning("graph: %s: extraction failed: %s", path, e)
                    facts = FileFacts(error=f"extraction failed: {type(e).__name__}: {e}")
                _put_facts(conn, path, part, facts, files)
                _put_file(conn, path, part, st.st_size, mtime, digest,
                          "error" if facts.error else "ok", facts.error)
                parsed += 1
                changed = True
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        errors = tuple(r[0] for r in conn.execute(
            "SELECT path FROM files WHERE status='error' ORDER BY path"))
        pending_history = history.cochange(store, conn, config, deadline)
        pending_history += history.link_sessions(store, conn, config, parts, deadline)
        _record_timing(conn, started, parsed, changed, was_empty and changed,
                       bool(pending or pending_history))
        return parsed, pending, errors, pending_history
    finally:
        conn.close()


def claim_path(store: StorePaths) -> Path:
    """Present from the moment a refresh starts the graph worker until that worker tries the
    lock. While it is there, no other refresh starts one."""
    return store.index / "graph.worker"


def _spawn_worker(store: StorePaths) -> None:
    """Start the graph worker to finish what a budgeted refresh left, unless one is on its way."""
    from wscontext.capture import worker  # loads the capture modules only when a worker starts

    claim = FileLock(claim_path(store), stale_after=CLAIM_STALE_SECONDS, timeout=0)
    try:
        claim.__enter__()
    except LockTimeout:
        return
    try:
        worker.spawn_worker(["refresh", str(store.root)], store.root, module=WORKER)
    except Exception as e:  # the answer is still good; the next refresh carries on
        claim.__exit__(None, None, None)
        log.warning("graph: starting the graph worker failed: %s", e)


def _run(store: StorePaths, config: Config, budget_ms: int | None, lock_timeout: float,
         wipe: bool) -> Freshness:
    enabled = config.graph.languages
    parts, unavailable = load_parts(enabled)
    off = tuple(n for n in REGISTRY if n not in enabled)
    try:
        with FileLock(db.lock_path(store), stale_after=LOCK_STALE_SECONDS, timeout=lock_timeout):
            if wipe:
                db.remove(store)
            parsed, pending, errors, pending_history = _walk(store, config, parts, budget_ms)
    except LockTimeout:
        return Freshness(complete=False, busy=True, unavailable=tuple(unavailable), off=off)
    complete = not pending and not pending_history
    if budget_ms is not None and not complete and parts:
        # after the lock is released, so the worker can take it
        _spawn_worker(store)
    return Freshness(complete=complete, pending_files=pending,
                     parse_errors=errors, unavailable=tuple(unavailable), off=off, parsed=parsed,
                     pending_history=pending_history)


def refresh(store: StorePaths, config: Config, budget_ms: int | None = None) -> Freshness:
    """Bring the graph up to the working tree, stopping once `budget_ms` is spent (at least one
    file is parsed per call) and starting the graph worker to finish. Never waits for the lock:
    when another writer holds it, the graph is left as it is and the result is `busy`."""
    return _run(store, config, budget_ms, lock_timeout=0, wipe=False)


def rebuild(store: StorePaths, config: Config) -> Freshness:
    """Delete the graph file, then a full refresh."""
    return _run(store, config, None, lock_timeout=REBUILD_LOCK_TIMEOUT, wipe=True)
