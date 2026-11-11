"""The detached Git-hook worker: checkpoint records, their history commits, history pushes.

Run as `python -m wscontext.githooks.worker <action> <repo-root> [<arg>]`.
Actions: `checkpoint <sha>`, `rewrite <queue-file>`, `push`.
"""

import sys
import time
from pathlib import Path

from wscontext.capture.state import git_value
from wscontext.config import Config, load_config
from wscontext.githooks.handlers import SHA, commit_links
from wscontext.gitutil import git
from wscontext.history import sync
from wscontext.history.branch import commit_paths, read_tip
from wscontext.logs import HOOKS_LOG, append_log, now_iso
from wscontext.redact.account import with_account_ids
from wscontext.store.locks import FileLock
from wscontext.store.paths import StorePaths, is_safe_name
from wscontext.store.records import (CheckpointRecord, RecordError, read_checkpoint,
                                     write_checkpoint)


def _diff_stats(store: StorePaths, sha: str) -> tuple[int, int, int]:
    """Files changed, insertions and deletions against the first parent."""
    parent = git_value(store, "rev-parse", "--verify", "--quiet", f"{sha}^1")
    trees = [parent, sha] if parent else ["--root", "--no-commit-id", sha]
    out = git("diff-tree", "-r", "-z", "--numstat", *trees, cwd=store.root)
    files = insertions = deletions = 0
    for entry in out.split("\0"):
        added, _, rest = entry.partition("\t")
        deleted, sep, _ = rest.partition("\t")
        if not sep:
            continue
        files += 1
        insertions += int(added) if added.isdigit() else 0  # `-` for binary files
        deletions += int(deleted) if deleted.isdigit() else 0
    return files, insertions, deletions


def _read(store: StorePaths, sha: str | None) -> CheckpointRecord | None:
    if sha is None:
        return None
    try:
        return read_checkpoint(store, sha)
    except (OSError, RecordError):
        return None


def write_linked_checkpoint(store: StorePaths, config: Config, sha: str, author: str,
                            sessions: list[str], rewritten_from: str | None) -> Path | None:
    """Write the checkpoint for `sha`, merged with any existing one and the rewritten commit's.

    `post-commit` and `post-rewrite` both fire for an amend or a rebased pick, in either
    order; merging keeps both contributions. Returns None for a commit with no session.
    """
    ids = set(sessions)
    old = _read(store, rewritten_from)
    existing = _read(store, sha)
    for rec in (old, existing):
        if rec is not None:
            ids.update(rec.session_ids)
    ids = {i for i in ids if is_safe_name(i)}
    if not ids:
        return None
    files, insertions, deletions = _diff_stats(store, sha)
    return write_checkpoint(store, CheckpointRecord(
        sha=sha, author=author, session_ids=sorted(ids),
        created_at=existing.created_at if existing else now_iso(), files_changed=files,
        insertions=insertions, deletions=deletions,
        rewritten_from=(existing.rewritten_from if existing else None) or rewritten_from), config)


def _checkpoints(store: StorePaths, config: Config, action: str, arg: str) -> int:
    if action == "checkpoint":
        if not SHA.fullmatch(arg):
            raise ValueError("not a commit SHA")
        pairs: list[tuple[str | None, str]] = [(None, arg)]
    else:
        queue = Path(arg).resolve()
        if queue.parent != (store.state / "rewrites").resolve():
            raise ValueError("rewrite queue outside the store")
        pairs = []
        for line in queue.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) >= 2 and SHA.fullmatch(fields[0]) and SHA.fullmatch(fields[1]):
                pairs.append((fields[0], fields[1]))
    links = commit_links(store, [new for _, new in pairs])
    written: list[Path] = []
    for old, new in pairs:
        if new not in links:
            continue
        author, sessions = links[new]
        path = write_linked_checkpoint(store, config, new, author, sessions, old)
        if path is not None and path not in written:
            written.append(path)
    if written:
        commit_paths(store, written, f"wscontext: checkpoint {arg}" if action == "checkpoint"
                     else f"wscontext: {len(written)} rewritten checkpoints")
    if action == "rewrite":
        queue.unlink(missing_ok=True)
    return len(written)


def run(action: str, root: str, arg: str = "") -> None:
    store = StorePaths.resolve(Path(root))
    config = with_account_ids(load_config(store.root), store)
    started = time.monotonic()
    entry: dict = {"ts": now_iso(), "event": f"worker:git-{action}", "ok": True}
    try:
        if action == "push":
            errors: list[str] = []
            entry["ok"] = sync.push(store, config, errors)  # never raises
            if not entry["ok"] and not errors and read_tip(store) is None:
                entry["ok"], entry["skipped"] = True, "no history yet"
            elif not entry["ok"]:
                entry["error"] = errors[0] if errors else "push did not complete"
        elif action in {"checkpoint", "rewrite"}:
            # post-commit and post-rewrite workers overlap on an amend; serialize the merges.
            with FileLock(store.state / "locks" / "githooks.lock", stale_after=300, timeout=120):
                entry["checkpoints"] = _checkpoints(store, config, action, arg)
        else:
            raise ValueError(f"unknown worker action {action!r}")
    except Exception as e:
        entry["ok"] = False
        entry["error"] = f"{type(e).__name__}: {e}"
    entry["ms"] = round((time.monotonic() - started) * 1000)
    try:
        append_log(store, config, HOOKS_LOG, entry)
    except Exception:
        pass


def main(argv: list[str]) -> int:
    try:
        run(*argv[:3])
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
