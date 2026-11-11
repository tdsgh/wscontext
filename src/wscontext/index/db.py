"""The index database: one SQLite file in WAL mode with a schema version in `meta`."""

import sqlite3
from pathlib import Path

from wscontext.store.paths import StorePaths

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sources(path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                     digest TEXT NOT NULL);
CREATE TABLE sessions(session_id TEXT PRIMARY KEY, branch TEXT, author TEXT, started_at TEXT,
                      ended_at TEXT, ended_by TEXT, title TEXT NOT NULL DEFAULT '',
                      files TEXT NOT NULL DEFAULT '', commits TEXT NOT NULL DEFAULT '');
CREATE TABLE commits(sha TEXT PRIMARY KEY, author TEXT, created_at TEXT, branch TEXT,
                     session_ids TEXT NOT NULL DEFAULT '', files TEXT NOT NULL DEFAULT '');
CREATE VIRTUAL TABLE docs_fts USING fts5(
    kind UNINDEXED, ref UNINDEXED, session_id UNINDEXED, turn UNINDEXED,
    prompt, reply, tools, message, tokenize='porter unicode61');
"""


class IndexUnavailable(Exception):
    """A read-only connection found no usable index file."""


def db_path(store: StorePaths) -> Path:
    return store.index / "history.db"


def _version(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row[0]) if row else None
    except sqlite3.DatabaseError:
        return None


def _remove(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(path) + suffix).unlink()
        except FileNotFoundError:
            pass


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def connect(store: StorePaths, readonly: bool = False) -> sqlite3.Connection:
    """Open the index. A writer must hold `index/index.lock`; it recreates the file when the
    schema version differs. A read-only connection never waits on writers (WAL) and raises
    `IndexUnavailable` for a missing or mismatched file."""
    path = db_path(store)
    if readonly:
        if not path.is_file():
            raise IndexUnavailable(str(path))
        conn = _open(path)
        conn.execute("PRAGMA query_only=ON")
        if _version(conn) != SCHEMA_VERSION:
            conn.close()
            raise IndexUnavailable(f"{path}: schema version mismatch")
        return conn
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _open(path)
    if _version(conn) != SCHEMA_VERSION:
        conn.close()
        _remove(path)
        conn = _open(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.execute("INSERT INTO meta VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
    conn.execute("PRAGMA journal_mode=WAL")
    return conn
