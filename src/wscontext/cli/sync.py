"""`ctx sync [--setup [--remote NAME] | --status | --dry-run]`: share the history branch.

`ctx sync` and the `pre-push` hook are the only places that push it (ADR-008).
"""

import re

from wscontext.cli.init import default_config_toml
from wscontext.cli.output import Reply, error
from wscontext.config import Config, load_config
from wscontext.gitutil import GitError, git
from wscontext.history import sync
from wscontext.history.branch import HISTORY_REF, LEGACY_REF, read_tip
from wscontext.store.paths import StorePaths
from wscontext.store.records import atomic_write

SYNC_SECTION = re.compile(r"^\[sync\][ \t]*$", re.M)
NEXT_SECTION = re.compile(r"^\[", re.M)
REMOTE_LINE = re.compile(r"^[ \t]*remote[ \t]*=.*$", re.M)


def add_arguments(parser) -> None:
    parser.add_argument("--setup", action="store_true",
                        help="turn sharing on by naming a Git remote")
    parser.add_argument("--remote", help="with --setup: the remote's name, as `git remote` lists it")
    parser.add_argument("--status", action="store_true", help="how far local history is ahead of or behind the remote")
    parser.add_argument("--dry-run", action="store_true", help="list the sessions a push would send")


def set_remote(text: str, name: str) -> str:
    """`text` with `[sync] remote` set to `name`, in place or as a new line under `[sync]`."""
    line = f'remote = "{name}"'
    head = SYNC_SECTION.search(text)
    if head is None:
        return text.rstrip("\n") + f"\n\n[sync]\n{line}\n"
    nxt = NEXT_SECTION.search(text, head.end())
    body_end = nxt.start() if nxt else len(text)
    body = text[head.end():body_end]
    if REMOTE_LINE.search(body):
        body = REMOTE_LINE.sub(lambda _: line, body, count=1)
    else:
        body = "\n" + line + body
    return text[:head.end()] + body + text[body_end:]


def _legacy_removed(store: StorePaths, remote: str, before: str | None) -> list[str]:
    """A line when the remote's stage-1 branch was seen at `before` and its tracking ref is gone now."""
    if before is not None and read_tip(store, sync.legacy_tracking_ref(remote)) is None:
        return [f"the stage-1 history branch {LEGACY_REF} was removed from {remote}; "
                f"history now lives at {HISTORY_REF} there"]
    return []


def _setup(args, store: StorePaths) -> Reply:
    remotes = git("remote", cwd=store.root).split()
    name = args.remote
    if name is None:
        if len(remotes) != 1:
            return error("name the remote with --remote; configured remotes: "
                         + (", ".join(remotes) or "none"), args.json)
        name = remotes[0]
    if name not in remotes:
        hint = " (a remote name, not a URL: add it with `git remote add`)" if re.search(r"[/:@.]", name) else ""
        return error(f"{name!r} is not a configured Git remote{hint}; configured: "
                     + (", ".join(remotes) or "none"), args.json)
    path = store.committed / "config.toml"
    text = path.read_text(encoding="utf-8") if path.exists() else default_config_toml()
    atomic_write(path, set_remote(text, name).encode("utf-8"))
    lines = [f"sync remote set to {name} in .wscontext/config.toml; commit that file so teammates inherit it"]
    legacy = read_tip(store, sync.legacy_tracking_ref(name))
    try:
        changed = sync.fetch(store, load_config(store.root), force=True)
        lines.append("history fetched" + (" and materialized" if changed else ": nothing new"))
        lines += _legacy_removed(store, name, legacy)
    except GitError as e:
        lines.append(f"history fetch failed: {e}")
    lines.append("History is pushed with your own `git push` and by `ctx sync`.")
    return Reply("\n".join(lines))


def run(args, store: StorePaths, config: Config) -> Reply:
    if args.setup:
        return _setup(args, store)
    if config.sync.remote is None:
        return error("sharing is off: no [sync] remote in .wscontext/config.toml. "
                     "Run `ctx sync --setup`", args.json)
    pending = sync.pending(store, config)
    if args.status or args.dry_run:
        status = sync.status(store, config)
        lines = [f"remote {status.remote}: {status.ahead} ahead, {status.behind} behind (as of the last fetch)",
                 f"sessions a push would send: {len(pending)}" + (": " + ", ".join(pending) if pending else "")]
        if read_tip(store, sync.legacy_tracking_ref(config.sync.remote)):
            lines.append(f"{config.sync.remote} still has the stage-1 history branch {LEGACY_REF} "
                         "(as of the last fetch); the next push removes it once local history contains it")
        return Reply("\n".join(lines))
    if read_tip(store) is None:
        return Reply("no history yet: nothing to push")
    legacy = read_tip(store, sync.legacy_tracking_ref(config.sync.remote))
    if sync.push(store, config):
        lines = [f"pushed history to {config.sync.remote} ({len(pending)} sessions sent)",
                 *_legacy_removed(store, config.sync.remote, legacy)]
        return Reply("\n".join(lines))
    return error(f"push to {config.sync.remote} failed; see `git remote -v`, then rerun", args.json)
