"""Store layout: committed `.wscontext/` plus the local store under the Git common dir."""

import re
from dataclasses import dataclass
from pathlib import Path

from wscontext.gitutil import git

# Every file and directory name the store writes: session IDs, SHAs, dates.
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def is_safe_name(name: str) -> bool:
    return bool(_SAFE_NAME.fullmatch(name))


@dataclass(frozen=True)
class StorePaths:
    root: Path
    committed: Path
    local: Path
    raw: Path
    records: Path
    index: Path
    state: Path
    logs: Path

    @classmethod
    def resolve(cls, cwd: Path) -> "StorePaths":
        out = git("rev-parse", "--show-toplevel", "--git-common-dir", cwd=cwd)
        top, common = out.splitlines()[:2]
        root = Path(top)
        common_dir = Path(common)
        if not common_dir.is_absolute():
            common_dir = Path(cwd) / common_dir
        local = common_dir.resolve() / "wscontext"
        return cls(
            root=root,
            committed=root / ".wscontext",
            local=local,
            raw=local / "raw",
            records=local / "records",
            index=local / "index",
            state=local / "state",
            logs=local / "logs",
        )
