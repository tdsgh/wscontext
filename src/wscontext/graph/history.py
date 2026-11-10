"""Git history in the graph: files that change together, and the sessions behind each function.

Both read Git and the store's `records/` only, and both run inside `build.refresh` under the graph
lock, after the file walk.
"""

import hashlib
import json
import re
import sqlite3
import time
from collections import Counter
from itertools import combinations
from pathlib import PurePosixPath

from wscontext.config import Config
from wscontext.gitutil import GitError, git, git_bytes
from wscontext.graph import parse
from wscontext.graph.languages.base import LanguagePart
from wscontext.graph.scope import MAX_FILE_BYTES, excluded
from wscontext.store.paths import StorePaths
from wscontext.store.records import RecordError, read_checkpoint, session_path

# a commit touching more files than this is a bulk change (rename, reformat) and says nothing
# about which files belong together
BULK_FILES = 30
# the window grows by extension; past this many times `cochange_commits` it is recounted
WINDOW_SLACK = 2
# commits written per transaction when linking sessions
LINK_BATCH = 50
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _set_meta(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    conn.executemany("INSERT OR REPLACE INTO meta VALUES(?, ?)", list(values.items()))


def _head(store: StorePaths) -> str | None:
    try:
        return git("rev-parse", "--verify", "-q", "HEAD", cwd=store.root).strip()
    except GitError:
        return None


def _is_ancestor(store: StorePaths, old: str, new: str) -> bool:
    try:
        git_bytes("merge-base", "--is-ancestor", old, new, cwd=store.root)
        return True
    except GitError:  # 1: not an ancestor; 128: the commit is gone
        return False


def _commit_files(store: StorePaths, *rev_args: str) -> list[list[str]]:
    """The file lists of non-merge commits, newest first. Paths Git quotes are left out."""
    out = git_bytes("-c", "core.quotepath=off", "log", "--no-merges", "--no-renames",
                    "--name-only", "--format=%x01", *rev_args, cwd=store.root)
    commits = []
    for block in out.decode("utf-8", "replace").split("\x01")[1:]:
        commits.append([p for p in block.split("\n") if p and not p.startswith('"')])
    return commits


def cochange(store: StorePaths, conn: sqlite3.Connection, config: Config,
             deadline: float | None = None) -> int:
    """Count the file pairs changed by the same commit over the last `cochange_commits`
    non-merge commits. Returns 1 when the budget was spent before starting, else 0."""
    head = _head(store)
    meta = dict(conn.execute("SELECT key, value FROM meta"))
    last = meta.get("cochange_head")
    exclude = list(config.graph.exclude)
    exclude_digest = hashlib.sha256(json.dumps(exclude).encode()).hexdigest()
    same_scope = meta.get("cochange_exclude") == exclude_digest
    if head is None or (head == last and same_scope):
        return 0
    if deadline is not None and time.monotonic() >= deadline:
        return 1
    limit = config.graph.cochange_commits
    examined = int(meta.get("cochange_examined", "0"))
    if last and same_scope and _is_ancestor(store, last, head) and examined < limit * WINDOW_SLACK:
        commits = _commit_files(store, f"{last}..{head}")
        examined += len(commits)
        full = False
    else:
        commits = _commit_files(store, "-n", str(limit), head)
        examined = len(commits)
        full = True
    pairs: Counter[tuple[str, str]] = Counter()
    per_file: Counter[str] = Counter()
    for files in commits:
        # the bulk cut counts only paths the graph keeps, so a vendored directory cannot hide
        # the real commits it was committed with
        files = sorted({p for p in files if not excluded(p, exclude)})
        if not files or len(files) > BULK_FILES:
            continue
        per_file.update(files)
        pairs.update(combinations(files, 2))
    conn.execute("BEGIN IMMEDIATE")
    try:
        if full:
            conn.execute("DELETE FROM cochange")
            conn.execute("DELETE FROM cochange_commits")
        conn.executemany(
            "INSERT INTO cochange(a, b, count) VALUES(?,?,?) "
            "ON CONFLICT(a, b) DO UPDATE SET count = count + excluded.count",
            [(a, b, n) for (a, b), n in pairs.items()])
        conn.executemany(
            "INSERT INTO cochange_commits(path, commits) VALUES(?,?) "
            "ON CONFLICT(path) DO UPDATE SET commits = commits + excluded.commits",
            list(per_file.items()))
        _set_meta(conn, {"cochange_head": head, "cochange_examined": str(examined),
                         "cochange_exclude": exclude_digest})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return 0


def _changed_files(store: StorePaths, sha: str) -> list[tuple[str, str, list[tuple[int, int]]]]:
    """(path, new blob ID, changed new-side line ranges) for each file the commit added or
    modified. A range of deleted lines is the line before the deletion."""
    out = git_bytes("-c", "core.quotepath=off", "diff-tree", "-r", "-p", "--raw", "--full-index",
                    "--no-renames", "--root", "--no-commit-id", "-U0", sha, cwd=store.root)
    blobs: dict[str, str] = {}
    ranges: dict[str, list[tuple[int, int]]] = {}
    path = None
    for line in out.decode("utf-8", "replace").split("\n"):
        if line.startswith(":"):
            meta, _, name = line.partition("\t")
            fields = meta.split()
            if name.startswith('"') or fields[4] == "D" or fields[1] == "160000":
                continue
            blobs[name] = fields[3]
        elif line.startswith("+++ "):
            path = line[4:].rstrip("\t")
            path = path[2:] if path.startswith("b/") else None
        elif line.startswith("@@") and path:
            m = _HUNK.match(line)
            if m:
                start, count = int(m.group(1)), int(m.group(2) or 1)
                ranges.setdefault(path, []).append(
                    (start, start + count - 1) if count else (max(start, 1), max(start, 1)))
    return [(p, b, ranges.get(p, [])) for p, b in blobs.items()]


def _owners(part: LanguagePart, data: bytes, path: str) -> list[str]:
    """The innermost definition's qualname for each line of the file ('' outside any)."""
    facts = parse.facts(part, data, path)
    owners = [""] * (data.count(b"\n") + 2)
    for d in sorted(facts.definitions, key=lambda d: d.start - d.end):  # outermost first
        for line in range(d.start, min(d.end, len(owners) - 1) + 1):
            owners[line - 1] = d.qualname
    return owners


def _sessions_present(conn: sqlite3.Connection, store: StorePaths) -> dict[str, bool]:
    def exists(session_id: str) -> bool:
        try:
            return session_path(store, session_id).is_file()
        except RecordError:
            return False

    return {s: exists(s) for (s,) in conn.execute("SELECT DISTINCT session_id FROM touched")}


def link_sessions(store: StorePaths, conn: sqlite3.Connection, config: Config,
                  parts: list[LanguagePart], deadline: float | None = None) -> int:
    """Link each session to the definitions its commits changed. Returns the number of commits
    left for a later refresh because the budget ran out. A commit not present in this clone is
    tried again at every refresh but is not counted: it may never arrive."""
    by_ext = {ext: p for p in parts for ext in p.extensions}
    checkpoints = store.records / "checkpoints"
    shas = sorted(p.stem for p in checkpoints.glob("*.json")) if checkpoints.is_dir() else []

    # rows of a session whose record is gone go, and so does its mark on the commit
    gone = {s for s, there in _sessions_present(conn, store).items() if not there}
    done = {sha: status for sha, status in conn.execute("SELECT sha, status FROM touched_done")}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for session_id in gone:
            conn.execute("DELETE FROM touched WHERE session_id=?", (session_id,))
        for sha, status in done.items():
            kept = sorted(set(status.split(",")) - gone)
            if status != "merge" and kept != status.split(","):
                done[sha] = ",".join(kept)
                conn.execute("UPDATE touched_done SET status=? WHERE sha=?", (done[sha], sha))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    todo: dict[str, list[str]] = {}
    for sha in shas:
        if done.get(sha) == "merge":
            continue
        try:
            wanted = read_checkpoint(store, sha).session_ids
        except (RecordError, OSError):
            continue
        have = set(done.get(sha, "").split(","))
        added = []
        for s in wanted:
            if s in have:
                continue
            try:
                if session_path(store, s).is_file():
                    added.append(s)
            except RecordError:
                pass
        if added:
            todo[sha] = added
    if not todo:
        return 0

    checked = git_bytes("cat-file", "--batch-check", input="".join(f"{s}\n" for s in todo)
                        .encode(), cwd=store.root).decode().split("\n")
    present = [f[0] for f in (line.split() for line in checked)
               if len(f) == 3 and f[1] == "commit"]
    if not present:
        return 0
    info = git_bytes("log", "--no-walk=unsorted", "--stdin", "--format=%H|%cI|%P",
                     input="".join(f"{s}\n" for s in present).encode(), cwd=store.root)
    commits = {}
    for line in info.decode().split("\n"):
        if line:
            sha, date, parents = line.split("|")
            commits[sha] = (date, len(parents.split()))

    cache: dict[str, list[str]] = {}  # blob ID -> innermost definition per line
    left = 0
    batch = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for i, (sha, (date, parents)) in enumerate(commits.items()):
            if deadline is not None and time.monotonic() >= deadline:
                left += len(commits) - i
                break
            if batch >= LINK_BATCH:
                conn.execute("COMMIT")
                conn.execute("BEGIN IMMEDIATE")
                batch = 0
            batch += 1
            if parents > 1:
                conn.execute("INSERT OR REPLACE INTO touched_done VALUES(?, 'merge')", (sha,))
                continue
            rows: set[tuple[str, str]] = set()
            for path, blob, ranges in _changed_files(store, sha):
                rows.add((path, ""))
                part = by_ext.get(PurePosixPath(path).suffix.lower())
                if part is None or not ranges:
                    continue
                if blob not in cache:
                    if len(cache) >= 256:
                        cache.clear()
                    data = git_bytes("cat-file", "blob", blob, cwd=store.root)
                    try:
                        cache[blob] = [] if len(data) > MAX_FILE_BYTES else _owners(
                            part, data, path)
                    except Exception:  # an unparsable blob links the file only
                        cache[blob] = []
                owners = cache[blob]
                for start, end in ranges:
                    rows.update((path, owners[n - 1]) for n in range(start, end + 1)
                                if n <= len(owners) and owners[n - 1])
            sessions = todo[sha]
            for session_id in sessions:
                conn.execute("DELETE FROM touched WHERE session_id=? AND sha=?",
                             (session_id, sha))
                conn.executemany("INSERT INTO touched VALUES(?,?,?,?,?)",
                                 [(session_id, sha, p, q, date) for p, q in sorted(rows)])
            have = set(done.get(sha, "").split(",")) - {""}
            conn.execute("INSERT OR REPLACE INTO touched_done VALUES(?,?)",
                         (sha, ",".join(sorted(have | set(sessions)))))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return left
