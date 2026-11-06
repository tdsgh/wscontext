"""`ctx doctor`: the setup checklist, one line per item with the fix for each failure."""

import json
import os
import re
import sqlite3
from pathlib import Path

from wscontext.cli.init import claude_block, current_block, launcher_path
from wscontext.cli.output import Reply
from wscontext.config import Config
from wscontext.githooks.install import CHAINED_SUFFIX, HOOKS, hooks_dir, _is_shim
from wscontext.gitutil import GitError, git
from wscontext.history.branch import HISTORY_REF, LEGACY_REF, read_tip
from wscontext.history.sync import legacy_tracking_ref
from wscontext.logs import HOOKS_LOG
from wscontext.stats.data import _index, collect, read_log
from wscontext.stats.render import _warning_line
from wscontext.store.paths import StorePaths
from wscontext.store.records import read_session

OK, WARN, FAIL = "ok", "warn", "fail"
SHIM_LAUNCHER = re.compile(r"\bl='([^']*)'")
REINIT = "run `ctx init` in this repository"


def _shim_launchers(directory: Path) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for hook in HOOKS:
        path = directory / hook
        if _is_shim(path):
            found = SHIM_LAUNCHER.search(path.read_text(encoding="utf-8", errors="replace"))
            out[hook] = found.group(1) if found else None
    return out


def _plugin(args) -> list[dict]:
    root = Path(args.plugin_root) if args.plugin_root else None
    if root is None:
        return [_item("plugin", WARN, "not run through the plugin launcher",
                      "run `ctx doctor` as the plugin's `ctx` command")]
    try:
        version = json.loads((root / ".claude-plugin" / "plugin.json").read_text())["version"]
    except (OSError, ValueError, KeyError):
        version = "unknown"
    items = [_item("plugin", OK, f"version {version} at {root}")]
    data = Path(args.plugin_data) if args.plugin_data else None
    if data is not None:
        stamp = data / "venv" / ".lock-hash"
        ready = stamp.exists() and stamp.read_text().startswith(f"{root} ")
        items.append(_item("environment", OK if ready else FAIL,
                           f"uv environment ready in {data / 'venv'}" if ready else
                           f"uv environment in {data / 'venv'} is missing or out of date",
                           "run any `ctx` command from the plugin; it builds the environment (needs uv and network)"))
    return items


def _item(name: str, status: str, detail: str, fix: str = "") -> dict:
    return {"check": name, "status": status, "detail": detail, **({"fix": fix} if status != OK else {})}


def _git_hooks(args, store: StorePaths) -> list[dict]:
    directory, managed = hooks_dir(store.root)
    if not managed:
        return [_item("git hooks", WARN, f"{directory} is not wscontext's to edit",
                      f"add the lines `{REINIT}` prints to the hooks there")]
    found = _shim_launchers(directory)
    missing = [h for h in HOOKS if h not in found]
    if not found:
        return [_item("git hooks", FAIL, "not installed", REINIT)]
    items = [_item("git hooks", OK if not missing else FAIL,
                   f"{len(found)} of {len(HOOKS)} installed" + (f"; missing: {', '.join(missing)}" if missing else ""),
                   REINIT)]
    chained = [h for h in found if (directory / (h + CHAINED_SUFFIX)).exists()]
    items.append(_item("chained hooks", OK, ", ".join(chained) if chained else "no existing hooks to chain"))
    stable = launcher_path(args.plugin_data) if args.plugin_data else None
    for path in sorted({p for p in found.values() if p}):
        problem = fix = ""
        if not os.access(path, os.X_OK):
            problem, fix = "does not exist or is not executable", REINIT
        elif "/plugins/cache/" in path.replace("\\", "/"):
            problem, fix = "points into the versioned plugin cache and stops working after a plugin update", REINIT
        elif stable is not None and Path(path) != stable:
            problem, fix = f"is not this plugin's stable launcher {stable}", REINIT
        else:
            # the stable launcher keeps `plugin-root` beside its `bin/`; a custom launcher has none
            record = Path(path).parent.parent / "plugin-root"
            if record.exists() and not (Path(record.read_text()) / "bin" / "ctx").exists():
                problem, fix = (f"names a plugin root that is gone ({record.read_text()})",
                                "run any `ctx` command from the plugin, which refreshes it")
        items.append(_item("git hook launcher", FAIL if problem else OK, f"{path} {problem}".strip(), fix))
    if None in found.values():
        items.append(_item("git hook launcher", FAIL, "a shim has no launcher path", REINIT))
    return items


def _claude_hooks(store: StorePaths) -> dict:
    runs = [r for r in read_log(store, HOOKS_LOG, None) if not str(r.get("event", "")).startswith("git:")]
    if not runs:
        return _item("claude code hooks", WARN, "no run recorded in this repository",
                     "start a Claude Code session here; if still empty, check the plugin is enabled (`/plugin`) and `/hooks` lists wscontext")
    failures = sum(r.get("ok") is False for r in runs)
    return _item("claude code hooks", OK if not failures else WARN,
                 f"{len(runs)} runs, {failures} failed, last {runs[-1].get('ts')}",
                 "see `ctx stats hooks` for the failures")


def _store(store: StorePaths) -> dict:
    probe = store.local
    while not probe.exists():
        probe = probe.parent
    ok = os.access(probe, os.W_OK)
    return _item("store", OK if ok else FAIL, f"{store.local}" + ("" if store.local.exists() else " (created on first capture)"),
                 f"make {probe} writable")


def _sync(store: StorePaths, config: Config) -> dict:
    remote = config.sync.remote
    if remote is None:
        return _item("sync", OK, "off (optional): history stays on this machine")
    if remote not in git("remote", cwd=store.root).split():
        return _item("sync", FAIL, f"[sync] remote {remote!r} is not a Git remote name",
                     "run `ctx sync --setup --remote <name>`")
    return _item("sync", OK, f"sharing through {remote}")


def _fts5(config: Config) -> dict:
    try:
        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite3.Error:
        return _item("search engine", FAIL if config.retrieval.enabled else WARN,
                     f"SQLite {sqlite3.sqlite_version} in this Python has no FTS5, so `ctx find` cannot run",
                     "use a Python build whose SQLite includes FTS5 (the python.org, uv and Homebrew builds do)")
    return _item("search engine", OK, f"SQLite {sqlite3.sqlite_version} with FTS5")


def _has_copy(store: StorePaths, session_id: str) -> bool:
    """Whether a transcript copy was made: a raw copy exists, or finalization recorded its size
    (the record outlives pruning of raw/; SessionStart writes a record before any copy)."""
    if any((store.raw / "sessions").glob(f"*-{session_id}.jsonl")):
        return True
    try:
        return read_session(store, session_id).transcript_bytes is not None
    except (OSError, ValueError):
        return False


def _search_index(store: StorePaths, config: Config) -> dict:
    if not config.retrieval.enabled:
        return _item("search index", OK, "not used: [retrieval] enabled = false")
    ix = _index(store)
    if not ix["present"]:
        if any(_has_copy(store, f.stem) for f in (store.records / "sessions").glob("*.json")):
            return _item("search index", WARN, "missing, although sessions are captured",
                         "run `ctx rebuild` (or any `ctx find`, which builds what is missing)")
        return _item("search index", OK, "not built yet: built on the first capture")
    detail = (f"{ix['sessions']} sessions, {ix['commits']} commits, {ix['documents']} documents; "
              f"lag {ix['lag']} sources behind captured history")
    return _item("search index", OK if not ix["lag"] else WARN, detail,
                 "the next capture or `ctx find` refreshes it; `ctx rebuild` rebuilds it from scratch")


def _history_layout(store: StorePaths, config: Config) -> dict:
    notes = []
    if read_tip(store, LEGACY_REF):
        notes.append(f"local stage-1 branch {LEGACY_REF} still exists; it moves into {HISTORY_REF} "
                     "on the next capture or `ctx sync`")
    remote = config.sync.remote
    if remote and read_tip(store, legacy_tracking_ref(remote)):
        notes.append(f"{remote} still has the stage-1 branch {LEGACY_REF} (as of the last fetch); "
                     "the next `ctx sync` push removes it once local history contains it")
    if notes:
        return _item("history ref", WARN, "; ".join(notes),
                     "run `ctx sync` to finish the move" if remote else "any capture finishes the move")
    return _item("history ref", OK, f"{HISTORY_REF}" + ("" if read_tip(store) else " (no history yet)"))


def _block(store: StorePaths, config: Config) -> dict:
    path = store.root / "CLAUDE.md"
    block = current_block(path.read_text(encoding="utf-8", errors="replace")) if path.exists() else None
    if block is None:
        return _item("claude.md block", WARN, "no wscontext block in CLAUDE.md", REINIT)
    on = config.retrieval.enabled
    if ("ctx find" in block) != on:
        return _item("claude.md block", WARN,
                     f"the block {'lacks' if on else 'has'} the `ctx find` trigger, but retrieval is {'on' if on else 'off'}",
                     REINIT)
    if block != claude_block(on):
        return _item("claude.md block", WARN, "the block is out of date", REINIT)
    return _item("claude.md block", OK, "matches the retrieval switch " + ("(on)" if on else "(off, no `ctx find` trigger)"))


def _size_warnings(store: StorePaths, config: Config) -> list[dict]:
    try:
        found = collect(store, config, None)["warnings"]
    except (GitError, OSError):
        return [_item("size limits", WARN, "could not measure the store", "run `ctx stats data`")]
    return [_item("size limits", WARN, _warning_line(w), "see `ctx stats data`; lower `capture.tool_result_max_chars` "
                  "to cut long tool results, or prune with `retention.raw_days`") for w in found] \
        or [_item("size limits", OK, "no session or store over its limit")]


def _stale_tracking(store: StorePaths) -> list[dict]:
    remotes = set(git("remote", cwd=store.root).split())
    stale: dict[str, list[str]] = {}
    for ref in git("for-each-ref", "--format=%(refname)", "refs/wscontext/remotes/", cwd=store.root).split():
        name, _, leaf = ref.removeprefix("refs/wscontext/remotes/").rpartition("/")
        if leaf in ("history", "legacy-history") and name not in remotes:
            stale.setdefault(name, []).append(ref)
    return [_item("tracking refs", WARN, f"{', '.join(refs)} track remote {name!r}, which no longer exists",
                  " && ".join(f"git update-ref -d {r}" for r in refs)) for name, refs in stale.items()]


def add_arguments(parser) -> None:
    pass


def run(args, store: StorePaths, config: Config) -> Reply:
    enabled = (store.committed / "config.toml").exists()
    items = _plugin(args)
    items.append(_item("repository", OK if enabled else FAIL,
                       ".wscontext/config.toml present" if enabled else ".wscontext/config.toml is missing",
                       REINIT))
    items += _git_hooks(args, store)
    items += [_claude_hooks(store), _store(store), _sync(store, config)]
    if enabled:
        items += [_fts5(config), _search_index(store, config), _history_layout(store, config),
                  _block(store, config), *_size_warnings(store, config), *_stale_tracking(store)]
    code = int(any(i["status"] == FAIL for i in items))
    if args.json:
        return Reply(json.dumps({"command": "ctx doctor", "ok": not code, "checks": items}, indent=2), code, report=True)
    lines = []
    for i in items:
        lines.append(f"[{i['status'].upper():>4}] {i['check']}: {i['detail']}")
        if "fix" in i:
            lines.append(f"       fix: {i['fix']}")
    return Reply("\n".join(lines), code, report=True)
