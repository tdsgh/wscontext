"""Query the index: one bm25 ranking over exchanges and commit messages."""

import re
import sqlite3
from dataclasses import dataclass

# weights per FTS column, in table order; unindexed columns weigh 0
_WEIGHTS = "0, 0, 0, 0, 4.0, 2.0, 1.0, 3.0"
_FETCH_FACTOR = 50
# NLTK's English stopword list; `\w+` splits "doesn't" into "doesn" and "t", so both halves are here
_STOPWORDS = frozenset("""
a about above after again against ain all am an and any are aren as at be because been before
being below between both but by can couldn d did didn do does doesn doing don down during each
few for from further had hadn has hasn have haven having he her here hers herself him himself
his how i if in into is isn it its itself just ll m ma me mightn more most mustn my myself
needn no nor not now o of off on once only or other our ours ourselves out over own re s same
shan she should shouldn so some such t than that the their theirs them themselves then there
these they this those through to too under until up ve very was wasn we were weren what when
where which while who whom why will with won wouldn y you your yours yourself yourselves
""".split())


@dataclass
class Hit:
    kind: str  # "session" or "commit"
    ref: str  # session id or commit sha
    session_id: str | None
    turn: int | None  # best exchange of a session; None for a commit or a subagent exchange
    snippet: str
    score: float  # bm25, lower is better
    date: str | None
    branch: str | None
    author: str | None
    title: str
    active: bool  # session not finalized yet


def _match(tokens: list[str], op: str) -> str:
    return f" {op} ".join('"' + t + '"' for t in tokens)


def search(conn: sqlite3.Connection, text: str, *, branch: str | None = None,
           author: str | None = None, since: str | None = None, file: str | None = None,
           kind: str | None = None, limit: int = 10) -> list[Hit]:
    """Quoted terms joined by AND; OR when AND finds fewer than `limit`. Stopwords are dropped,
    and AND leaves out terms that bm25 gives no weight, unless that leaves nothing. One hit per
    session (its best exchange) and per commit; a matching commit linked to a session hit follows
    it. `since` is an ISO date or timestamp; `file` matches as a substring of a touched path."""
    tokens = re.findall(r"\w+", text)
    tokens = [t for t in tokens if t.lower() not in _STOPWORDS] or tokens
    if not tokens or limit < 1:
        return []
    # bm25 weighs a term found in half the rows or more as zero
    rows = conn.execute("SELECT count(*) FROM docs_fts").fetchone()[0]
    weighted = [t for t in tokens if 2 * conn.execute(
        "SELECT count(*) FROM docs_fts WHERE docs_fts MATCH ?", (_match([t], "OR"),)).fetchone()[0]
        < rows] or tokens
    hits = _run(conn, _match(weighted, "AND"), branch, author, since, file, kind, limit)
    if len(hits) < limit and len(tokens) > 1:
        seen = {(h.kind, h.ref) for h in hits}
        more = _run(conn, _match(tokens, "OR"), branch, author, since, file, kind, limit)
        # AND matches rank first; OR adds the rest
        hits += [h for h in more if (h.kind, h.ref) not in seen][: limit - len(hits)]
    if kind == "session":
        return hits
    # a commit matching a weighted term ranks no lower than right after its best-ranked session
    sessions = [h.ref for h in hits if h.kind == "session"]
    linked = {sid: [c for c in shas.split("\n") if c] for sid, shas in conn.execute(
        f"SELECT session_id, commits FROM sessions WHERE session_id IN "
        f"({','.join('?' * len(sessions))})", sessions)}
    wanted = [c for shas in linked.values() for c in shas]
    found = {h.ref: h for h in _run(conn, _match(weighted, "OR"), branch, author, since, file,
                                    "commit", len(wanted), refs=wanted)} if wanted else {}
    out: dict[tuple[str, str], Hit] = {}
    for h in hits:
        out.setdefault((h.kind, h.ref), h)
        for c in sorted((found[c] for c in linked.get(h.ref, ()) if c in found),
                        key=lambda c: c.score):
            out.setdefault(("commit", c.ref), c)
    return list(out.values())[:limit]


def _run(conn, match, branch, author, since, file, kind, limit, refs=None) -> list[Hit]:
    where = ["docs_fts MATCH ?"]
    params: list = [match]
    if refs is not None:
        where.append(f"d.ref IN ({','.join('?' * len(refs))})")
        params.extend(refs)
    if kind:
        where.append("d.kind = ?")
        params.append(kind)
    if branch:
        where.append("COALESCE(s.branch, c.branch) = ?")
        params.append(branch)
    if author:
        where.append("COALESCE(s.author, c.author) = ?")
        params.append(author)
    if since:
        where.append("COALESCE(s.ended_at, s.started_at, c.created_at) >= ?")
        params.append(since)
    if file:
        where.append("instr(COALESCE(s.files, c.files), ?) > 0")
        params.append(file)
    sql = f"""
        SELECT d.kind, d.ref, d.session_id, d.turn, snippet(docs_fts, -1, '', '', '...', 24),
               bm25(docs_fts, {_WEIGHTS}) AS score,
               COALESCE(s.started_at, c.created_at), COALESCE(s.branch, c.branch),
               COALESCE(s.author, c.author), COALESCE(s.title, ''),
               s.session_id IS NOT NULL AND s.ended_at IS NULL
        FROM docs_fts d
        LEFT JOIN sessions s ON d.kind = 'session' AND s.session_id = d.ref
        LEFT JOIN commits c ON d.kind = 'commit' AND c.sha = d.ref
        WHERE {' AND '.join(where)}
        ORDER BY score LIMIT ?"""
    params.append(limit * _FETCH_FACTOR)
    out: list[Hit] = []
    seen: set[tuple[str, str]] = set()
    for row in conn.execute(sql, params):
        key = (row[0], row[1])
        if key in seen:
            continue
        seen.add(key)
        turn = row[3] if row[0] == "session" else None
        out.append(Hit(row[0], row[1], row[2] or None, turn, row[4], row[5], row[6], row[7],
                       row[8], row[9], bool(row[10])))
        if len(out) == limit:
            break
    return out


def brief_rows(conn: sqlite3.Connection, branch: str | None, files: list[str],
               exclude: list[str] | str = ()) -> dict:
    """Up to three recent sessions on `branch` and up to three, not among those, that touched
    `files` (most overlap first). `exclude` is session ids to leave out, such as the current one.
    Each row: session_id, title, started_at, ended_at, commits (list of SHAs), files."""
    skip = {exclude} if isinstance(exclude, str) else set(exclude)

    def row(r) -> dict:
        return {"session_id": r[0], "title": r[1], "started_at": r[2], "ended_at": r[3],
                "commits": [c for c in r[4].split("\n") if c],
                "files": [f for f in r[5].split("\n") if f]}

    cols = "session_id, title, started_at, ended_at, commits, files"
    recent: list[dict] = []
    if branch:
        for r in conn.execute(
                f"SELECT {cols} FROM sessions WHERE branch = ? "
                "ORDER BY COALESCE(ended_at, started_at) DESC", (branch,)):
            if r[0] not in skip and len(recent) < 3:
                recent.append(row(r))
    taken = skip | {r["session_id"] for r in recent}
    wanted = set(files)
    scored = []
    if wanted:
        for r in conn.execute(
                f"SELECT {cols} FROM sessions WHERE files != '' "
                "ORDER BY COALESCE(ended_at, started_at) DESC"):
            if r[0] in taken:
                continue
            overlap = len(wanted & set(r[5].split("\n")))
            if overlap:
                scored.append((overlap, row(r)))
    scored.sort(key=lambda p: -p[0])  # stable: recency breaks ties
    return {"recent": recent, "files": [r for _, r in scored[:3]]}

