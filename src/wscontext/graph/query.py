"""Read the graph: definitions by name, a file's outline, a target resolved to definitions."""

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Symbol:
    id: int
    path: str
    name: str
    qualname: str
    kind: str
    start: int
    end: int
    signature: str
    # qualname of the enclosing definition
    parent: str | None


@dataclass(frozen=True)
class ImportEdge:
    # the importing file
    path: str
    spec: str
    # the imported repository file; None when the import is not resolved in the repository
    target: str | None
    line: int


@dataclass(frozen=True)
class Outline:
    path: str
    language: str
    # ok, error or skipped
    status: str
    error: str | None
    definitions: list[Symbol]
    imports: list[ImportEdge]
    importers: list[ImportEdge]


@dataclass(frozen=True)
class Resolved:
    # the file the target names; None for a name looked up across the repository
    path: str | None
    # one definition, the candidates, or none; empty for a whole file
    symbols: list[Symbol]


_SYMBOLS = ("SELECT s.id, s.path, s.name, s.qualname, s.kind, s.start_line, s.end_line, "
            "s.signature, p.qualname FROM symbols s LEFT JOIN symbols p ON p.id = s.parent_id ")


def _symbols(conn: sqlite3.Connection, where: str, params: tuple) -> list[Symbol]:
    return [Symbol(*row) for row in conn.execute(_SYMBOLS + where, params)]


def definitions(conn: sqlite3.Connection, name: str, *, kind: str | None = None,
                limit: int | None = 20) -> list[Symbol]:
    """Definitions whose name or qualname is `name`, then those whose name contains it (any case),
    shortest first, then those whose qualname only contains it. `limit=None` returns all."""
    kind_sql, kind_args = (" AND s.kind = ?", (kind,)) if kind else ("", ())
    cap = -1 if limit is None else limit
    exact = _symbols(conn, "WHERE (s.name = ? OR s.qualname = ?)" + kind_sql
                     + " ORDER BY s.path, s.start_line LIMIT ?", (name, name, *kind_args, cap))
    if limit is not None and len(exact) >= limit:
        return exact
    rest = _symbols(conn, "WHERE (instr(lower(s.name), lower(?)) > 0 "
                    "OR instr(lower(s.qualname), lower(?)) > 0) "
                    "AND NOT (s.name = ? OR s.qualname = ?)" + kind_sql
                    + " ORDER BY instr(lower(s.name), lower(?)) = 0, length(s.name), s.path, "
                    "s.start_line LIMIT ?",
                    (name, name, name, name, *kind_args, name,
                     -1 if limit is None else limit - len(exact)))
    return exact + rest


def _edges(conn: sqlite3.Connection, column: str, path: str) -> list[ImportEdge]:
    return [ImportEdge(*row) for row in conn.execute(
        f"SELECT path, spec, target, line FROM imports WHERE {column} = ? "
        "ORDER BY path, line, target", (path,))]


def file_outline(conn: sqlite3.Connection, path: str) -> Outline | None:
    """A file's definitions in order, its imports, and the files importing it; None when the file
    is not in the graph."""
    row = conn.execute("SELECT language, status, error FROM files WHERE path = ?",
                       (path,)).fetchone()
    if row is None:
        return None
    return Outline(path=path, language=row[0], status=row[1], error=row[2],
                   definitions=_symbols(conn, "WHERE s.path = ? ORDER BY s.start_line, s.id",
                                        (path,)),
                   imports=_edges(conn, "path", path),
                   importers=[e for e in _edges(conn, "target", path) if e.path != path])


def _norm(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def resolve(conn: sqlite3.Connection, target: str) -> Resolved:
    """`path`, `path#qualname`, `#name` or a bare name. A bare target is a file when the graph
    holds it or it contains a `/`; `#name` and a bare name match a name or a qualname anywhere.
    In a file, a qualname match wins over a short-name match."""
    if "#" in target:
        path, _, name = target.partition("#")
        path = _norm(path)
    elif "/" in target or "\\" in target or conn.execute(
            "SELECT 1 FROM files WHERE path = ?", (_norm(target),)).fetchone():
        return Resolved(path=_norm(target), symbols=[])
    else:
        path, name = "", target
    if not path:
        return Resolved(path=None, symbols=_symbols(
            conn, "WHERE s.qualname = ? OR s.name = ? ORDER BY s.path, s.start_line",
            (name, name)))
    exact = _symbols(conn, "WHERE s.path = ? AND s.qualname = ? ORDER BY s.start_line",
                     (path, name))
    if exact or not name:
        return Resolved(path=path, symbols=exact)
    return Resolved(path=path, symbols=_symbols(
        conn, "WHERE s.path = ? AND (s.name = ? OR substr(s.qualname, -length(?)) = ?) "
        "ORDER BY s.start_line", (path, name, "." + name, "." + name)))


@dataclass(frozen=True)
class CoChange:
    path: str
    # commits that changed both files
    count: int
    # `count` as a share of the commits that changed the asked file
    share: float


@dataclass(frozen=True)
class SessionLink:
    session_id: str
    sha: str
    path: str
    # '' when the link is to the file as a whole
    qualname: str
    committed_at: str | None


def cochanged(conn: sqlite3.Connection, path: str, limit: int = 10,
              min_count: int = 2) -> list[CoChange]:
    """Files changed by the same commits as `path`, most often first."""
    commits = conn.execute("SELECT commits FROM cochange_commits WHERE path = ?",
                           (path,)).fetchone()
    if commits is None:
        return []
    rows = conn.execute(
        "SELECT CASE WHEN a = ?1 THEN b ELSE a END AS other, count FROM cochange "
        "WHERE (a = ?1 OR b = ?1) AND count >= ?2 ORDER BY count DESC, other LIMIT ?3",
        (path, min_count, -1 if limit is None else limit))
    return [CoChange(other, count, count / commits[0]) for other, count in rows]


def sessions_for(conn: sqlite3.Connection, path: str, qualname: str | None = None,
                 limit: int | None = None) -> list[SessionLink]:
    """The sessions whose commits changed a definition (`qualname`) or, with no qualname, any
    part of the file; newest commit first. A definition's links hold after its lines move."""
    where, args = ("path = ? AND qualname = ?", (path, qualname)) if qualname else (
        "path = ? AND qualname = ''", (path,))
    return [SessionLink(*row) for row in conn.execute(
        f"SELECT session_id, sha, path, qualname, committed_at FROM touched WHERE {where} "
        "ORDER BY committed_at DESC, sha, session_id LIMIT ?",
        (*args, -1 if limit is None else limit))]


@dataclass(frozen=True)
class Call:
    # the file and line of the call
    path: str
    line: int
    # the definition the call sits in; None for top-level code
    caller: Symbol | None


def callers_of(conn: sqlite3.Connection, name: str) -> list[Call]:
    """The calls made to the short name `name`, by path and line. Matching is by name only, so a
    call may be to any definition that shares it (see `name_count`)."""
    rows = conn.execute(
        "SELECT c.path, c.line, s.id, s.path, s.name, s.qualname, s.kind, s.start_line, "
        "s.end_line, s.signature, p.qualname FROM calls c "
        "LEFT JOIN symbols s ON s.id = c.caller_id LEFT JOIN symbols p ON p.id = s.parent_id "
        "WHERE c.name = ? ORDER BY c.path, c.line, s.id", (name,))
    return [Call(path, line, Symbol(*sym) if sym[0] is not None else None)
            for path, line, *sym in rows]


def name_count(conn: sqlite3.Connection, name: str) -> int:
    """How many definitions have the short name `name`."""
    return conn.execute("SELECT count(*) FROM symbols WHERE name = ?", (name,)).fetchone()[0]
