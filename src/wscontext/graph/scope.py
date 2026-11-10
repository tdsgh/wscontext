"""What the graph leaves out: excluded paths and files too large to parse."""

import fnmatch
from collections.abc import Sequence
from pathlib import PurePosixPath

# larger files are generated or minified code: recorded as skipped
MAX_FILE_BYTES = 1_048_576


def excluded(path: str, patterns: Sequence[str]) -> bool:
    """A pattern without `/` matches any path component (`*.min.js`, `vendor`); one with `/` is
    matched from the repository root, and a directory pattern covers everything under it."""
    for pattern in patterns:
        pattern = pattern.strip("/")
        if not pattern:
            continue
        if "/" not in pattern:
            if any(fnmatch.fnmatchcase(c, pattern) for c in path.split("/")):
                return True
        else:
            p = PurePosixPath(path)
            if p.full_match(pattern) or p.full_match(pattern + "/**"):
                return True
    return False
