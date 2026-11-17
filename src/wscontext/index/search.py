"""Query the index: one bm25 ranking over exchanges and commit messages."""

import math
import re
import sqlite3
from dataclasses import dataclass, replace

from wscontext.cli.output import CHARS_PER_TOKEN, estimate_tokens
from wscontext.index.db import EXCHANGE_KEYS, EXCHANGE_STATS, IndexUnavailable, connect
from wscontext.store.paths import StorePaths

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
# words a request is phrased with that name no topic; dropped from a prompt on top of _STOPWORDS
_FILLER = frozenset("""
able actually already also anyway anything could everything get gets going goes got instead just
know let lets like look made make makes maybe might must need needs perhaps please really same see
shall something still sure tell thank thanks thing things think try use used using want wanted wants
way well would
""".split())
_PROMPT_TERMS = 32  # distinct words of a prompt looked up, in the order they appear
_COMMON_SHARE = 0.25  # a word in more of the exchanges than this names no topic
_CANDIDATE_ROWS = 100  # exchanges whose length is read to settle the floor
_SEARCH_DEPTH = 20
# the floor, in nats: a match as strong as a hit's is expected by chance fewer than e**-_FLOOR times
_FLOOR = 1.0
_SAID = "{prompt reply} : "  # what was said in a session: not tool output, not commit messages


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


def prompt_hits(store: StorePaths, text: str, exclude: str | None = None, *, limit: int = 3,
                budget: int = 300) -> list[Hit]:
    """Sessions for a prompt brief: up to `limit` session hits for a prompt's distinctive words,
    in `search` order, leaving out session `exclude`; or none when no exchange clears the floor.
    `budget` caps the tokens of the hits' titles and snippets together: later hits are dropped and
    the first one's snippet is cut to fit. Reads the index read-only and never refreshes it; a
    missing, busy or unreadable index gives no hits.

    Distinctive words: not a stopword, not request filler, at least 3 characters, and said in at
    most a quarter of the indexed exchanges. The floor: one exchange of the session must share at
    least two of them, and that match must be unlikely by chance. A word said in `df` of `n`
    exchanges is in a given one with chance df/n, raised for an exchange longer than average; the
    chance that some `m` of the prompt's `k` words meet in any one exchange is then about
    n * C(k, m) * product(df/n). The hit clears the floor when that is below 1/e."""
    words: list[str] = []
    for w in re.findall(r"\w+", text.lower()):
        if len(w) >= 3 and w not in _STOPWORDS and w not in _FILLER and w not in words:
            words.append(w)
            if len(words) == _PROMPT_TERMS:
                break
    if len(words) < 2 or limit < 1:
        return []
    try:
        conn = connect(store, readonly=True)
    except (IndexUnavailable, sqlite3.Error, OSError):
        return []
    try:
        conn.execute("PRAGMA busy_timeout=0")  # the hook cannot wait on a writer
        return _prompt_hits(conn, words, exclude, limit, budget)
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _prompt_hits(conn, words, exclude, limit, budget) -> list[Hit]:
    stats = dict(conn.execute(f"SELECT key, value FROM meta WHERE key IN "
                              f"({','.join('?' * len(EXCHANGE_KEYS))})", EXCHANGE_KEYS))
    if len(stats) == len(EXCHANGE_KEYS):
        n, chars = (int(stats[k]) for k in EXCHANGE_KEYS)
    else:  # an index built before refresh kept them
        n, chars = conn.execute(EXCHANGE_STATS).fetchone()
    if n < 2 or not chars:
        return []
    average = chars / n
    rows: dict[str, set[int]] = {}  # distinctive word -> the exchanges that said it
    for w in words:
        match = _SAID + _match([w], "OR")
        df = conn.execute("SELECT count(*) FROM docs_fts WHERE docs_fts MATCH ?",
                          (match,)).fetchone()[0]
        if not 0 < df <= _COMMON_SHARE * n:
            continue
        said = {r for r, in conn.execute("SELECT rowid FROM docs_fts WHERE docs_fts MATCH ?",
                                          (match,))}
        # the index stems words: "warn" and "warning" are one term, counted once
        if not any(said == seen and (w.startswith(o) or o.startswith(w))
                   for o, seen in rows.items()):
            rows[w] = said
    k = len(rows)
    if k < 2:
        return []
    idf = {w: math.log(n / len(said)) for w, said in rows.items()}
    shared: dict[int, list[str]] = {}
    for w, said in rows.items():
        for r in said:
            shared.setdefault(r, []).append(w)

    def evidence(ws: list[str], cut: float = 0.0) -> float:
        return (sum(max(0.0, idf[w] - cut) for w in ws) - math.log(n)
                - math.log(math.comb(k, len(ws))))

    # without the length correction, which only lowers it, evidence is an upper bound
    bound = {r: evidence(ws) for r, ws in shared.items() if len(ws) >= 2}
    candidates = sorted((r for r in bound if bound[r] >= _FLOOR), key=bound.get,
                        reverse=True)[:_CANDIDATE_ROWS]
    if not candidates:
        return []
    passed: set[str] = set()
    for r, ref, length in conn.execute(
            f"SELECT rowid, ref, length(prompt) + length(reply) FROM docs_fts "
            f"WHERE rowid IN ({','.join('?' * len(candidates))})", candidates):
        if ref != exclude and evidence(shared[r], max(0.0, math.log(length / average))) >= _FLOOR:
            passed.add(ref)
    if not passed:
        return []
    out: list[Hit] = []
    used = 0
    for hit in search(conn, " ".join(rows), kind="session", limit=_SEARCH_DEPTH):
        if hit.ref not in passed:
            continue
        cost = estimate_tokens(hit.title) + estimate_tokens(hit.snippet)
        if used + cost > budget:
            room = (budget - estimate_tokens(hit.title)) * CHARS_PER_TOKEN
            if not out and room >= 0:
                out.append(replace(hit, snippet=hit.snippet[:room - 3].rstrip() + "..."
                                   if room > 3 else ""))
            break
        out.append(hit)
        used += cost
        if len(out) == limit:
            break
    return out
