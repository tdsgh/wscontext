"""One handler per Git hook. Each stays cheap: checkpoints and history pushes go to the worker."""

import os
import re
import time
from datetime import datetime
from pathlib import Path

from wscontext.capture.state import active_path, git_value, read_json, turn_state_path, worktree_key
from wscontext.capture.worker import spawn_worker
from wscontext.config import Config
from wscontext.gitutil import git
from wscontext.redact.redactor import redact_text
from wscontext.store.paths import StorePaths, is_safe_name
from wscontext.store.records import atomic_write, checkpoint_path

TRAILER = "AI-Session"
WORKER = "wscontext.githooks.worker"
SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# The characters Git chooses from for `core.commentChar = auto`.
AUTO_COMMENT_CHARS = "#;@!$%^&|:"


def spawn(store: StorePaths, *args: str) -> None:
    spawn_worker([args[0], str(store.root), *args[1:]], store.root, module=WORKER)


def _timestamp(value) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


def active_sessions(store: StorePaths, config: Config) -> list[str]:
    """Sessions active in this worktree with activity inside the staleness window, oldest first."""
    sessions = read_json(active_path(store)).get(worktree_key(store, config))
    if not isinstance(sessions, dict):
        return []
    cutoff = time.time() - config.capture.stale_session_minutes * 60
    fresh = []
    for sid, started in sessions.items():
        if not is_safe_name(sid):
            continue
        last = _timestamp(started)
        try:
            # every prompt and tool call rewrites the turn state
            last = max(last, turn_state_path(store, sid).stat().st_mtime)
        except OSError:
            pass
        if last >= cutoff:
            fresh.append((_timestamp(started), sid))
    return [sid for _, sid in sorted(fresh)]


def commit_links(store: StorePaths, revs: list[str]) -> dict[str, tuple[str, list[str]]]:
    """For each commit, by full SHA: its author email and the session IDs in its trailers."""
    out = git("log", "--no-walk=unsorted", "--no-show-signature", "--stdin",
              f"--format=%H%n%ae%n%(trailers:key={TRAILER},valueonly)%x00",
              cwd=store.root, input="".join(f"{rev}\n" for rev in revs))
    links = {}
    for chunk in out.split("\0"):
        lines = chunk.strip("\n").split("\n")
        if len(lines) >= 2 and SHA.fullmatch(lines[0]):
            links[lines[0]] = (lines[1], [s.strip() for s in lines[2:] if is_safe_name(s.strip())])
    return links


def _rebasing(store: StorePaths) -> bool:
    out = git("rev-parse", "--path-format=absolute", "--git-path", "rebase-merge",
              "--git-path", "rebase-apply", cwd=store.root)
    return any(Path(p).is_dir() for p in out.splitlines())


def _comment_prefixes(store: StorePaths) -> tuple[str, ...]:
    value = (git_value(store, "config", "--get", "core.commentString")
             or git_value(store, "config", "--get", "core.commentChar") or "#")
    return tuple(AUTO_COMMENT_CHARS) if value == "auto" else (value,)


def _is_empty(message: str, comment: tuple[str, ...]) -> bool:
    for line in message.splitlines():
        if line.startswith(comment):
            if " >8 " in line:
                break  # scissors: the rest is the `commit -v` diff
            continue
        if line.strip():
            return False
    return True


def link_sessions(store: StorePaths, config: Config, args: list[str], stdin: str) -> None:
    """`prepare-commit-msg` and `commit-msg`: an `AI-Session` trailer per active session."""
    if not args:
        return
    sessions = active_sessions(store, config)
    # A rebase or `git am` replays commits that already carry their own trailers.
    if not sessions or _rebasing(store):
        return
    message = Path(args[0]).resolve()
    # Git does not count a trailer-only message as empty: an aborted editor would commit it.
    # The editor flow is linked by `commit-msg` instead, once the message has text.
    if _is_empty(message.read_text(encoding="utf-8", errors="replace"), _comment_prefixes(store)):
        return
    git("interpret-trailers", "--in-place", "--if-exists", "addIfDifferent",
        *(f"--trailer={TRAILER}: {sid}" for sid in sessions), str(message), cwd=store.root)


def post_commit(store: StorePaths, config: Config, args: list[str], stdin: str) -> None:
    for sha, (_, sessions) in commit_links(store, ["HEAD"]).items():
        if sessions:
            spawn(store, "checkpoint", sha)


def post_rewrite(store: StorePaths, config: Config, args: list[str], stdin: str) -> None:
    """Old -> new pairs on stdin; queued for the worker when either side is linked."""
    pairs = []
    for line in stdin.splitlines():
        fields = line.split()
        if len(fields) >= 2 and SHA.fullmatch(fields[0]) and SHA.fullmatch(fields[1]):
            pairs.append((fields[0], fields[1]))
    if not pairs:
        return
    if not (any(checkpoint_path(store, old).exists() for old, _ in pairs)
            or any(ids for _, ids in commit_links(store, [new for _, new in pairs]).values())):
        return
    queue = store.state / "rewrites" / f"{time.time_ns()}-{os.getpid()}.txt"
    text, _ = redact_text("".join(f"{old} {new}\n" for old, new in pairs), config)
    atomic_write(queue, text.encode("utf-8"))
    spawn(store, "rewrite", str(queue))


def pre_push(store: StorePaths, config: Config, args: list[str], stdin: str) -> None:
    if config.sync.remote:
        spawn(store, "push")


def post_merge(store: StorePaths, config: Config, args: list[str], stdin: str) -> None:
    """A merge commit made here is checkpointed like a `post-commit` one; the graph refresh lands in stage 3."""
    # `--squash` (argument "1") commits later through `post-commit`.
    if args[:1] == ["1"]:
        return
    # A fast-forward leaves HEAD at a commit this merge did not create: its first parent is
    # ORIG_HEAD only for a new merge commit, and a one-step fast-forward onto someone else's
    # merge commit is told apart by the reflog.
    out = git("rev-list", "--parents", "-1", "HEAD", cwd=store.root).split()
    if len(out) < 3 or out[1] != git("rev-parse", "--verify", "ORIG_HEAD", cwd=store.root).strip():
        return
    if git("reflog", "-1", "--format=%gs", "HEAD", cwd=store.root).strip().endswith("Fast-forward"):
        return
    post_commit(store, config, args, stdin)


def _graph_refresh(store: StorePaths, config: Config, args: list[str], stdin: str) -> None:
    """`post-checkout`: the code graph refresh lands in stage 3."""


HANDLERS = {
    "prepare-commit-msg": link_sessions,
    "commit-msg": link_sessions,
    "post-commit": post_commit,
    "post-rewrite": post_rewrite,
    "pre-push": pre_push,
    "post-checkout": _graph_refresh,
    "post-merge": post_merge,
}
