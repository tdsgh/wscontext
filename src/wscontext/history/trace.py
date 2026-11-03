"""Read-only Git queries behind `ctx why`: blame for a line range and commit metadata."""

import re
from dataclasses import dataclass, field

from wscontext.gitutil import GitError, git
from wscontext.store.paths import StorePaths, is_safe_name

TRAILER = "AI-Session"
UNCOMMITTED = "0" * 40
_HEADER = re.compile(r"([0-9a-f]{40}|[0-9a-f]{64}) \d+ (\d+)(?: \d+)?")


class NotTracked(Exception):
    """The file has no history: it is not in `HEAD`."""


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    date: str
    author: str
    subject: str
    sessions: list[str] = field(default_factory=list)


def blame(store: StorePaths, rel: str, start: int | None = None,
          end: int | None = None) -> dict[str, list[int]]:
    """Commit SHA -> the file's line numbers it last changed; uncommitted lines map to zeros.

    Raises NotTracked for a file Git has no history for, ValueError for a bad line range.
    """
    args = ["blame", "--porcelain"]
    if start is not None:
        args += ["-L", f"{start},{end if end is not None else start}"]
    try:
        out = git(*args, "--", rel, cwd=store.root)
    except GitError as e:
        if "no such path" in e.stderr:
            raise NotTracked(rel) from None
        if "has only" in e.stderr or "invalid line" in e.stderr:
            raise ValueError(e.stderr.strip()) from None
        raise
    lines: dict[str, list[int]] = {}
    for row in out.split("\n"):
        if row.startswith("\t"):
            continue
        m = _HEADER.fullmatch(row)
        if m:
            lines.setdefault(m.group(1), []).append(int(m.group(2)))
    return lines


def commits(store: StorePaths, shas: list[str]) -> dict[str, CommitInfo]:
    """Date, author, subject and `AI-Session` trailers of each commit, by full SHA."""
    if not shas:
        return {}
    out = git("log", "--no-walk=unsorted", "--no-show-signature", "--stdin",
              f"--format=%H%x1f%aI%x1f%ae%x1f%s%x1f%(trailers:key={TRAILER},valueonly,separator=%x2c)%x00",
              cwd=store.root, input="".join(f"{s}\n" for s in shas))
    found = {}
    for chunk in out.split("\0"):
        fields = chunk.strip("\n").split("\x1f")
        if len(fields) == 5:
            sessions = [s.strip() for s in fields[4].split(",") if is_safe_name(s.strip())]
            found[fields[0]] = CommitInfo(fields[0], fields[1], fields[2], fields[3], sessions)
    return found
