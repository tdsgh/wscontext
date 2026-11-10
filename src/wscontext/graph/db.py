"""The graph database: one SQLite file in WAL mode with a schema version in `meta`."""

import sqlite3
from pathlib import Path

from wscontext.store.paths import StorePaths

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE files(path TEXT PRIMARY KEY, language TEXT NOT NULL, lang_version INTEGER NOT NULL,
                   size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, digest TEXT NOT NULL,
                   status TEXT NOT NULL, error TEXT);
CREATE TABLE symbols(id INTEGER PRIMARY KEY, path TEXT NOT NULL, name TEXT NOT NULL,
                     qualname TEXT NOT NULL, kind TEXT NOT NULL, start_line INTEGER NOT NULL,
                     end_line INTEGER NOT NULL, signature TEXT NOT NULL, parent_id INTEGER);
CREATE INDEX symbols_path ON symbols(path);
CREATE INDEX symbols_name ON symbols(name);
CREATE INDEX symbols_qualname ON symbols(qualname);
CREATE TABLE imports(path TEXT NOT NULL, spec TEXT NOT NULL, names TEXT NOT NULL, target TEXT,
                     line INTEGER NOT NULL);
CREATE INDEX imports_path ON imports(path);
CREATE INDEX imports_target ON imports(target);
CREATE TABLE calls(path TEXT NOT NULL, caller_id INTEGER, name TEXT NOT NULL,
                   line INTEGER NOT NULL);
CREATE INDEX calls_path ON calls(path);
CREATE INDEX calls_name ON calls(name);
CREATE TABLE cochange(a TEXT NOT NULL, b TEXT NOT NULL, count INTEGER NOT NULL,
                      PRIMARY KEY(a, b));
CREATE TABLE cochange_commits(path TEXT PRIMARY KEY, commits INTEGER NOT NULL);
CREATE TABLE touched(session_id TEXT NOT NULL, sha TEXT NOT NULL, path TEXT NOT NULL,
                     qualname TEXT NOT NULL, committed_at TEXT);
CREATE INDEX touched_symbol ON touched(path, qualname);
CREATE INDEX touched_session ON touched(session_id);
CREATE TABLE touched_done(sha TEXT PRIMARY KEY, status TEXT NOT NULL);
"""


class GraphUnavailable(Exception):
    """A read-only connection found no usable graph file."""


def db_path(store: StorePaths) -> Path:
    return store.index / "graph.db"


def lock_path(store: StorePaths) -> Path:
    return store.index / "graph.lock"


def _version(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row[0]) if row else None
    except sqlite3.DatabaseError:
        return None


def remove(store: StorePaths) -> None:
    """Delete the graph file and its WAL. The caller holds `index/graph.lock`."""
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path(store)) + suffix).unlink(missing_ok=True)


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def connect(store: StorePaths, readonly: bool = False) -> sqlite3.Connection:
    """Open the graph. A writer must hold `index/graph.lock`; it recreates the file when the
    schema version differs. A read-only connection never waits on writers (WAL) and raises
    `GraphUnavailable` for a missing or mismatched file."""
    path = db_path(store)
    if readonly:
        if not path.is_file():
            raise GraphUnavailable(str(path))
        conn = _open(path)
        conn.execute("PRAGMA query_only=ON")
        if _version(conn) != SCHEMA_VERSION:
            conn.close()
            raise GraphUnavailable(f"{path}: schema version mismatch")
        return conn
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _open(path)
    if _version(conn) != SCHEMA_VERSION:
        conn.close()
        remove(store)
        conn = _open(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.execute("INSERT INTO meta VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def summary(conn: sqlite3.Connection) -> dict:
    """Counts by language and by status, the last build and refresh from `meta`, and the size."""
    by_language: dict[str, dict[str, int]] = {}
    for language, files in conn.execute(
            "SELECT language, count(*) FROM files GROUP BY language ORDER BY language"):
        by_language[language] = {"files": files, "symbols": 0, "imports": 0, "calls": 0}
    for table in ("symbols", "imports", "calls"):
        for language, n in conn.execute(
                f"SELECT f.language, count(*) FROM {table} t JOIN files f ON f.path = t.path "
                "GROUP BY f.language"):
            by_language[language][table] = n
    by_status = {"ok": 0, "error": 0, "skipped": 0}
    for status, n in conn.execute("SELECT status, count(*) FROM files GROUP BY status"):
        by_status[status] = n
    meta = dict(conn.execute("SELECT key, value FROM meta"))

    def last(kind: str) -> dict | None:
        if f"last_{kind}_at" not in meta:
            return None
        return {"at": meta[f"last_{kind}_at"], "ms": int(meta[f"last_{kind}_ms"]),
                "parsed": int(meta[f"last_{kind}_parsed"])}

    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    return {"by_language": by_language, "by_status": by_status, "last_build": last("build"),
            "last_refresh": last("refresh"), "bytes": pages * page_size}
