"""Claude Code's own folder, read only: where its transcripts are and when it deletes them.
Stdlib only. Nothing here opens a file for writing."""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CLEANUP_DAYS = 30
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


def config_dir() -> Path:
    """`$CLAUDE_CONFIG_DIR` when set, else `~/.claude`. The only place the environment is read."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def projects_dir() -> Path:
    return config_dir() / "projects"


def cleanup_days(repo_root: Path) -> tuple[int, Path | None]:
    """Claude Code's `cleanupPeriodDays` and the file it came from: the first integer of 1 or
    more in the repository's `.claude/settings.local.json`, `.claude/settings.json`, then
    `<config dir>/settings.json`. Otherwise 30 and None. Managed settings are not read."""
    for path in (repo_root / ".claude" / "settings.local.json",
                 repo_root / ".claude" / "settings.json",
                 config_dir() / "settings.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        value = data.get("cleanupPeriodDays") if isinstance(data, dict) else None
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            return value, path
    return DEFAULT_CLEANUP_DAYS, None


@dataclass
class Transcript:
    session_id: str
    path: Path
    cwd: str
    branch: str | None
    version: str | None
    first_ts: str | None
    mtime: float


def _read_head(path: Path) -> dict[str, str] | None:
    """The first `sessionId`, `gitBranch`, `version` and `timestamp` up to the first line
    with `cwd`, plus that `cwd`. None when no line carries one."""
    found: dict[str, str] = {}
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            for key in ("sessionId", "gitBranch", "version", "timestamp"):
                if key not in found and isinstance(obj.get(key), str):
                    found[key] = obj[key]
            if isinstance(obj.get("cwd"), str) and obj["cwd"]:
                found["cwd"] = obj["cwd"]
                return found
    return None


def transcripts() -> list[Transcript]:
    """Every `*.jsonl` directly inside a folder under `projects_dir()`. A file is skipped
    when it cannot be read, has no `cwd` line, has an unsafe name stem, or its stem is not
    its `sessionId`. Subagent files sit deeper and are not listed."""
    found: list[Transcript] = []
    try:
        folders = sorted(p for p in projects_dir().iterdir() if p.is_dir())
    except OSError:
        return []
    for folder in folders:
        try:
            files = sorted(folder.glob("*.jsonl"))
        except OSError:
            continue
        for path in files:
            if not SAFE_NAME.match(path.stem):
                continue
            try:
                if not path.is_file():
                    continue
                head = _read_head(path)
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if head is None or head.get("sessionId") != path.stem:
                continue
            found.append(Transcript(path.stem, path, head["cwd"], head.get("gitBranch"),
                                    head.get("version"), head.get("timestamp"), mtime))
    return found
