"""Build and refresh the index from the store's redacted `raw/` and `records/`.

Sources are keyed by store path with size, mtime and SHA-256; a session whose transcript or
record changed is re-indexed whole in one transaction, so re-running is idempotent.
"""

import hashlib
import logging
import re
import sqlite3
import tempfile
from collections.abc import Callable
from pathlib import Path

from wscontext.adapters.base import Turn
from wscontext.adapters.claude_code import ClaudeCodeAdapter
from wscontext.gitutil import GitError, git
from wscontext.history import branch as history
from wscontext.index.db import connect, db_path
from wscontext.store.locks import FileLock, LockTimeout
from wscontext.store.paths import StorePaths, is_safe_name
from wscontext.store.records import (RecordError, read_checkpoint, read_session)

log = logging.getLogger(__name__)

TITLE_CHARS = 80
LOCK_STALE_SECONDS = 600


def _lock(store: StorePaths, timeout: float) -> FileLock:
    return FileLock(store.index / "index.lock", stale_after=LOCK_STALE_SECONDS, timeout=timeout)


def _branch_paths(store: StorePaths) -> list[str]:
    tip = history.read_tip(store)
    if tip is None:
        return []
    try:
        return list(history.ls_blobs(store, tip))
    except GitError:
        return []


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Sources:
    """The `sources` table: which files, at which size and content, are already indexed."""

    def __init__(self, conn: sqlite3.Connection):
        self.rows = {p: (s, m, d) for p, s, m, d in conn.execute(
            "SELECT path, size, mtime_ns, digest FROM sources")}

    def check(self, store: StorePaths, key: str, data_fn: Callable[[], bytes | None] | None = None
              ) -> tuple[str, tuple[int, int, str] | None, bytes | None]:
        """Compare a source with its row: ("same"|"changed"|"gone"|"pruned", new row, data).
        "pruned" is a raw file missing locally that was indexed before: its rows stay."""
        path = store.local.joinpath(*key.split("/"))
        old = self.rows.get(key)
        try:
            st = path.stat()
        except FileNotFoundError:
            if old is not None:
                return "pruned", None, None
            data = data_fn() if data_fn else None
            if data is None:
                return "gone", None, None
            return "changed", (len(data), 0, _sha256(data)), data
        if old and old[0] == st.st_size and old[1] == st.st_mtime_ns:
            return "same", None, None
        data = path.read_bytes()
        row = (len(data), st.st_mtime_ns, _sha256(data))
        if old and old[2] == row[2]:
            return "touched", row, None
        return "changed", row, data


def _exchange_docs(turns: list[Turn], subagent: bool) -> list[tuple]:
    docs = []
    for t in turns:
        prompt = next((i.text for i in t.items if i.kind == "prompt"), "")
        reply = "\n".join(i.text for i in t.items if i.kind == "text" and i.text)
        tools = "\n".join(f"{i.name} {i.text}".strip() for i in t.items if i.kind == "tool_call")
        if prompt or reply or tools:
            docs.append((None if subagent else t.index, prompt, reply, tools))
    return docs


def _parse(data: bytes, sidechain: bool):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "t.jsonl"
        path.write_bytes(data)
        return ClaudeCodeAdapter().parse(path, sidechain=sidechain)


def _title(turns: list[Turn]) -> str:
    for t in turns:
        first = next((i.text for i in t.items if i.kind == "prompt"), "").strip()
        if first:
            return " ".join(first.split())[:TITLE_CHARS]
    return ""


def _session_keys(store: StorePaths, session_id: str, known: set[str], branch_paths
                  ) -> tuple[list[str], list[str]]:
    """(raw main copies, subagent copies) as store-relative keys: local, indexed or on the branch."""
    main_re = re.compile(rf"raw/sessions/\d{{4}}-\d\d-\d\d-{re.escape(session_id)}\.jsonl")
    sub_prefix = f"raw/subagents/{session_id}/"
    keys = {k for k in known if main_re.fullmatch(k) or k.startswith(sub_prefix)}
    for p in sorted((store.raw / "sessions").glob(f"????-??-??-{session_id}.jsonl")):
        keys.add(f"raw/sessions/{p.name}")
    for p in sorted((store.raw / "subagents" / session_id).glob("*.jsonl")):
        keys.add(f"{sub_prefix}{p.name}")
    if not any(main_re.fullmatch(k) for k in keys):  # nothing local or indexed: look at the branch
        keys |= {k for k in branch_paths() if main_re.fullmatch(k) or k.startswith(sub_prefix)}
    main = sorted(k for k in keys if k.startswith("raw/sessions/"))
    return main, sorted(k for k in keys if k.startswith(sub_prefix))


def _put_sources(conn, rows: dict[str, tuple[int, int, str]]) -> None:
    conn.executemany("INSERT OR REPLACE INTO sources VALUES(?, ?, ?, ?)",
                     [(k, *v) for k, v in rows.items()])


def _drop_session(conn: sqlite3.Connection, session_id: str, known: set[str]) -> None:
    conn.execute("DELETE FROM docs_fts WHERE kind='session' AND ref=?", (session_id,))
    conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
    gone = [k for k in known if k == f"records/sessions/{session_id}.json"
            or re.fullmatch(rf"raw/sessions/\d{{4}}-\d\d-\d\d-{re.escape(session_id)}\.jsonl", k)
            or k.startswith(f"raw/subagents/{session_id}/")]
    conn.executemany("DELETE FROM sources WHERE path=?", [(k,) for k in gone])


def _index_one(store: StorePaths, conn: sqlite3.Connection, sources: _Sources, session_id: str,
               branch_paths) -> bool:
    """Index one session if any source changed. True when anything was written."""
    rec_key = f"records/sessions/{session_id}.json"
    status, rec_row, _ = sources.check(store, rec_key)
    if status == "gone":
        if not conn.execute("SELECT 1 FROM sessions WHERE session_id=?", (session_id,)).fetchone():
            return False
        conn.execute("BEGIN IMMEDIATE")
        _drop_session(conn, session_id, set(sources.rows))
        conn.execute("COMMIT")
        return True
    try:
        record = read_session(store, session_id)
    except (OSError, RecordError) as e:
        log.warning("index: unreadable session record %s: %s", session_id, e)
        return False
    main_keys, sub_keys = _session_keys(store, session_id, set(sources.rows), branch_paths)
    new_rows: dict[str, tuple[int, int, str]] = {}
    if rec_row:
        new_rows[rec_key] = rec_row
    changed = status == "changed" or not conn.execute(
        "SELECT 1 FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    loaded: dict[str, bytes] = {}
    for key in main_keys + sub_keys:
        st, row, data = sources.check(store, key, lambda k=key: history.read_blob(store, k))
        if row:
            new_rows[key] = row
        if st == "changed":
            changed = True
            loaded[key] = data
    if not changed and not new_rows:
        return False

    docs: list[tuple] = []
    title: str | None = None
    if any(k in loaded for k in main_keys + sub_keys):
        # a changed transcript: rebuild every exchange from all copies still readable
        for key in main_keys + sub_keys:
            path = store.local.joinpath(*key.split("/"))
            data = loaded.get(key)
            if data is None and path.is_file():
                data = path.read_bytes()
            if data is None:
                data = history.read_blob(store, key)
            if data is None:
                continue
            is_sub = key in sub_keys
            parsed = _parse(data, sidechain=is_sub)
            if not parsed.supported:
                continue
            docs += _exchange_docs(parsed.turns, subagent=is_sub)
            if not is_sub and title is None:
                title = _title(parsed.turns)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if docs or loaded:
            conn.execute("DELETE FROM docs_fts WHERE kind='session' AND ref=?", (session_id,))
            conn.executemany(
                "INSERT INTO docs_fts(kind, ref, session_id, turn, prompt, reply, tools, message) "
                "VALUES('session', ?, ?, ?, ?, ?, ?, '')",
                [(session_id, session_id, *d) for d in docs])
        conn.execute(
            "INSERT INTO sessions(session_id, branch, author, started_at, ended_at, ended_by, "
            "title, files) VALUES(?,?,?,?,?,?,COALESCE(?, ''),?) "
            "ON CONFLICT(session_id) DO UPDATE SET branch=excluded.branch, "
            "author=excluded.author, started_at=excluded.started_at, ended_at=excluded.ended_at, "
            "ended_by=excluded.ended_by, files=excluded.files, "
            "title=COALESCE(?, sessions.title)",
            (session_id, record.branch, record.author, record.started_at, record.ended_at,
             getattr(record, "ended_by", None), title, "\n".join(record.files_touched), title))
        _put_sources(conn, new_rows)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return True


def _commit_messages(store: StorePaths, shas: list[str]) -> dict[str, str]:
    """Messages of the SHAs present locally, in two batched Git calls."""
    if not shas:
        return {}
    try:
        checked = git("cat-file", "--batch-check", cwd=store.root, input="\n".join(shas) + "\n")
    except GitError:
        return {}
    present = [line.split()[0] for line in checked.splitlines()
               if len(line.split()) >= 2 and line.split()[1] == "commit"]
    if not present:
        return {}
    try:
        out = git("log", "--no-walk=unsorted", "--stdin", "--format=%H%x00%B%x01",
                  cwd=store.root, input="\n".join(present) + "\n")
    except GitError:
        return {}
    msgs = {}
    for chunk in out.split("\x01"):
        sha, _, body = chunk.strip("\n").partition("\0")
        if sha:
            msgs[sha.strip()] = body.strip()
    return msgs


def _index_commits(store: StorePaths, conn: sqlite3.Connection, sources: _Sources,
                   only_session: str | None) -> tuple[int, int, int]:
    """Returns (indexed, removed, pending)."""
    directory = store.records / "checkpoints"
    on_disk = {p.stem for p in directory.glob("*.json")} if directory.is_dir() else set()
    rows: dict[str, tuple[int, int, str]] = {}
    records = {}
    for sha in sorted(on_disk):
        key = f"records/checkpoints/{sha}.json"
        st, row, _ = sources.check(store, key)
        have = conn.execute("SELECT 1 FROM commits WHERE sha=?", (sha,)).fetchone()
        if have and st in ("same", "touched"):
            if row:
                rows[key] = row  # same content, new mtime
            continue
        try:
            rec = read_checkpoint(store, sha)
        except (OSError, RecordError):
            continue
        if only_session and only_session not in rec.session_ids:
            continue
        records[sha] = rec
        if row:
            rows[key] = row
    messages = _commit_messages(store, list(records))
    removed = 0
    gone = [] if only_session else [
        r[0] for r in conn.execute("SELECT sha FROM commits") if r[0] not in on_disk]
    conn.execute("BEGIN IMMEDIATE")
    try:
        for sha in gone:
            conn.execute("DELETE FROM docs_fts WHERE kind='commit' AND ref=?", (sha,))
            conn.execute("DELETE FROM commits WHERE sha=?", (sha,))
            conn.execute("DELETE FROM sources WHERE path=?", (f"records/checkpoints/{sha}.json",))
            removed += 1
        indexed = 0
        for sha, rec in records.items():
            if sha not in messages:
                continue  # not in this clone yet: retried next time
            conn.execute("DELETE FROM docs_fts WHERE kind='commit' AND ref=?", (sha,))
            conn.execute(
                "INSERT INTO docs_fts(kind, ref, session_id, turn, prompt, reply, tools, message)"
                " VALUES('commit', ?, ?, NULL, '', '', '', ?)",
                (sha, rec.session_ids[0] if rec.session_ids else "", messages[sha]))
            conn.execute(
                "INSERT INTO commits(sha, author, created_at, session_ids) VALUES(?,?,?,?) "
                "ON CONFLICT(sha) DO UPDATE SET author=excluded.author, "
                "created_at=excluded.created_at, session_ids=excluded.session_ids",
                (sha, rec.author, rec.created_at, "\n".join(rec.session_ids)))
            indexed += 1
        _put_sources(conn, {k: v for k, v in rows.items()
                            if k.split("/")[-1][:-5] in messages
                            or k.split("/")[-1][:-5] not in records})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    pending = len([s for s in records if s not in messages])
    return indexed, removed, pending


def _link(conn: sqlite3.Connection) -> None:
    """Derive each commit's branch and files and each session's commit list from the rows."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        by_session: dict[str, list[str]] = {}
        for sha, sids, in conn.execute("SELECT sha, session_ids FROM commits ORDER BY created_at, sha"):
            files: set[str] = set()
            branch = None
            for sid in filter(None, sids.split("\n")):
                by_session.setdefault(sid, []).append(sha)
                row = conn.execute("SELECT branch, files FROM sessions WHERE session_id=?",
                                   (sid,)).fetchone()
                if row:
                    branch = branch or row[0]
                    files.update(f for f in row[1].split("\n") if f)
            conn.execute("UPDATE commits SET branch=?, files=? WHERE sha=?",
                         (branch, "\n".join(sorted(files)), sha))
        for sid, in conn.execute("SELECT session_id FROM sessions").fetchall():
            conn.execute("UPDATE sessions SET commits=? WHERE session_id=?",
                         ("\n".join(by_session.get(sid, [])), sid))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _work(store: StorePaths, session_id: str | None, result: dict) -> None:
    conn = connect(store)
    try:
        sources = _Sources(conn)
        on_disk = {p.stem for p in (store.records / "sessions").glob("*.json")
                   if is_safe_name(p.stem)}
        ids = [session_id] if session_id else sorted(on_disk)
        cache: list[list[str]] = []

        def branch_paths() -> list[str]:
            if not cache:
                cache.append(_branch_paths(store))
            return cache[0]

        for sid in ids:
            try:
                if _index_one(store, conn, sources, sid, branch_paths):
                    result["sessions_indexed"] += 1
            except Exception as e:  # one bad session never blocks the rest
                log.warning("index: session %s failed: %s", sid, e)
                result["lag"] += 1
        if not session_id:
            known = {r[0] for r in conn.execute("SELECT session_id FROM sessions")}
            for sid in sorted(known - on_disk):
                conn.execute("BEGIN IMMEDIATE")
                _drop_session(conn, sid, set(sources.rows))
                conn.execute("COMMIT")
                result["sessions_removed"] += 1
        sources = _Sources(conn)
        indexed, removed, pending = _index_commits(store, conn, sources, session_id)
        result.update(commits_indexed=indexed, commits_removed=removed, pending_commits=pending)
        result["lag"] += pending
        _link(conn)
    finally:
        conn.close()


def _run(store: StorePaths, session_id: str | None, lock_timeout: float) -> dict:
    result = {"sessions_indexed": 0, "sessions_removed": 0, "commits_indexed": 0,
              "commits_removed": 0, "pending_commits": 0, "lag": 0, "locked": False}
    try:
        with _lock(store, lock_timeout):
            _work(store, session_id, result)
    except LockTimeout:
        result.update(locked=True, lag=-1)
    return result


def refresh(store: StorePaths, lock_timeout: float = 60) -> dict:
    """The gap check: index what changed since the last run, drop rows of sessions whose record
    is gone. Returns counts plus `lag`, the sources still behind (-1 when another writer holds the
    lock past `lock_timeout`; the index is then as current as that writer makes it)."""
    return _run(store, None, lock_timeout)


def index_session(store: StorePaths, session_id: str, lock_timeout: float = 60) -> dict:
    """Refresh one session and the checkpoints that name it; same result shape as `refresh`."""
    if not is_safe_name(session_id):
        raise ValueError(f"unsafe session id: {session_id!r}")
    return _run(store, session_id, lock_timeout)


def rebuild(store: StorePaths, lock_timeout: float = 60) -> dict:
    """Delete the index file, then refresh."""
    try:
        with _lock(store, lock_timeout):
            for suffix in ("", "-wal", "-shm"):
                Path(str(db_path(store)) + suffix).unlink(missing_ok=True)
    except LockTimeout:
        return {"locked": True, "lag": -1}
    return refresh(store, lock_timeout)
