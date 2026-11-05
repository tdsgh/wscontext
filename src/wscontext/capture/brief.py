"""The session brief: prior work relevant to where a session starts. Stdlib only (hook path)."""

import sqlite3

from wscontext.capture.state import git_value
from wscontext.cli.output import (FOOTER, HEADER, Block, defang, estimate_tokens, fit)
from wscontext.config import Config
from wscontext.gitutil import GitError, git
from wscontext.index.db import IndexUnavailable, connect
from wscontext.index.search import brief_rows
from wscontext.store.paths import StorePaths

BUDGET_TOKENS = 500
COMMAND = "session brief"
TITLE_CHARS = 120
SHOWN = 3  # commits or files named per session


def _changed_files(store: StorePaths) -> list[str]:
    """Repo-relative paths with uncommitted changes, from `git status`."""
    try:
        out = git("status", "--porcelain", "-z", cwd=store.root)
    except (GitError, OSError):
        return []
    entries = out.split("\0")
    files = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        if len(entry) > 3 and not entry.endswith("/"):
            files.append(entry[3:])
        i += 2 if entry[:1] in "RC" else 1  # a rename or copy is followed by its source path
    return files


def _block(row: dict, extra_line: str) -> Block:
    sid = row["session_id"]
    title = " ".join((row["title"] or "(untitled)").split())
    if len(title) > TITLE_CHARS:
        title = title[:TITLE_CHARS - 3] + "..."
    body = [extra_line] if extra_line else []
    return Block("session", sid[:8], title, (row["started_at"] or "")[:10] or None, body,
                 [f"ctx show {sid[:8]}"])


def build(store: StorePaths, config: Config, session_id: str) -> str | None:
    """The brief for a session starting now, or None: retrieval or the brief is off, the index
    is missing or unreadable, or there is nothing relevant. Reads the index read-only and never
    refreshes it."""
    if not (config.retrieval.enabled and config.retrieval.brief):
        return None
    branch = git_value(store, "rev-parse", "--abbrev-ref", "HEAD")
    branch = None if branch == "HEAD" else branch
    files = _changed_files(store)
    try:
        conn = connect(store, readonly=True)
    except (IndexUnavailable, sqlite3.Error, OSError):
        return None
    try:
        rows = brief_rows(conn, branch, files, exclude=[session_id])
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    wanted = set(files)
    recent = [_block(r, "commits: " + ", ".join(c[:8] for c in r["commits"][:SHOWN])
                     if r["commits"] else "") for r in rows["recent"]]
    touched = [_block(r, "touched: " + ", ".join(sorted(wanted & set(r["files"]))[:SHOWN]))
               for r in rows["files"]]
    if not recent and not touched:
        return None
    headings = {"recent": f"Recent sessions on branch {branch}:",
                "files": "Earlier sessions that touched files you have uncommitted changes in:"}
    frame = estimate_tokens(HEADER.format(command=COMMAND) + FOOTER + "".join(headings.values()))
    kept, _ = fit(recent + touched, BUDGET_TOKENS - frame - 8)
    lines = [HEADER.format(command=COMMAND)]
    for name, group in (("recent", recent), ("files", touched)):
        shown = [b for b in group if any(b is k for k in kept)]
        if shown:
            lines += ["", defang(headings[name]), *(b.text() for b in shown)]
    lines += ["", FOOTER]
    return "\n".join(lines)
