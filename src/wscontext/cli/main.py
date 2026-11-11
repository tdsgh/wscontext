"""The `ctx` entry point. Imports only the module of the subcommand that runs.

`hook`, `worker` and `git-hook` are internal commands for the Claude Code and Git hooks:
they take the stdlib-only path and never load argparse, the store commands or the output code.
"""

import argparse
import importlib
import sys
import time
from pathlib import Path

# Read and admin commands: name -> module with `add_arguments(parser)` and `run(args, store, config)`.
COMMANDS = {
    "why": "wscontext.cli.why",
    "sessions": "wscontext.cli.sessions",
    "show": "wscontext.cli.show",
    "forget": "wscontext.cli.forget",
    "stats": "wscontext.cli.stats",
    "init": "wscontext.cli.init",
    "doctor": "wscontext.cli.doctor",
    "uninstall": "wscontext.cli.uninstall",
    "sync": "wscontext.cli.sync",
    "find": "wscontext.cli.find",
    "code": "wscontext.cli.code",
    "impact": "wscontext.cli.impact",
    "brief": "wscontext.cli.brief",
    "rebuild": "wscontext.cli.rebuild",
    "eval": "wscontext.cli.eval",
}
# Read commands that start the throttled background fetch when a sync remote is set (ADR-008).
# They also spawn the `sweep` worker when a session is idle or the last sweep is a day old.
FETCHING = {"why", "sessions", "show", "stats", "find", "brief", "code", "impact"}
# Commands that read history refs: an upgraded repository with no write since still has its
# history under the stage-1 ref until this moves it.
MIGRATING = {"forget", "sync", "stats", "sessions"}
INTERNAL = {"hook", "worker", "git-hook"}
# Options before the command name: the launcher's (kept for init and doctor), then the shared ones.
LAUNCHER_OPTIONS = {"--plugin-root", "--plugin-data"}
SHARED_FLAGS = {"--json", "--full"}


def _internal(name: str, argv: list[str]) -> int:
    if name == "hook":
        from wscontext.capture import dispatch
        return dispatch.main(argv)
    if name == "worker":
        from wscontext.capture import worker
        return worker.main(argv)
    from wscontext.githooks import dispatch as githook
    return githook.main(argv)


def _parser(name: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"ctx {name}")
    parser.add_argument("--json", action="store_true", help="the same content as JSON")
    parser.add_argument("--limit", type=int, help="show at most this many results")
    parser.add_argument("--full", action="store_true", help="no token budget, full detail")
    return parser


def _usage() -> str:
    return "usage: ctx [--json] [--limit N] [--full] <command> ...\ncommands: " + ", ".join(COMMANDS)


def _log_call(store, config, argv: list[str], reply, started: float) -> None:
    """One `ctx-calls` line per call; the stats join it with the hook-side line by argv and time."""
    from wscontext.logs import CTX_CALLS_LOG, append_log, now_iso
    try:
        append_log(store, config, CTX_CALLS_LOG, {
            "ts": now_iso(), "source": "cli", "cwd": str(Path.cwd()), "argv": argv, "exit": reply.code,
            "ms": round((time.monotonic() - started) * 1000),
            "output_bytes": len(reply.text.encode("utf-8")),
            "results": reply.results, "flags": reply.flags})
    except OSError:
        pass


def _spawn(store, args: list[str]) -> None:
    """Detached and unawaited; the worker applies its own throttle."""
    from wscontext.capture import worker
    try:
        worker.spawn_worker(args, store.root)
    except OSError:
        pass


def _migrate(store) -> None:
    from wscontext.gitutil import GitError
    from wscontext.history.branch import migrate_legacy
    try:
        migrate_legacy(store)
    except (GitError, OSError):
        pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    lead: list[str] = []
    launcher: dict[str, str] = {}
    while argv and argv[0].startswith("-"):
        option = argv.pop(0)
        if option in LAUNCHER_OPTIONS:
            if argv:
                launcher[option.removeprefix("--plugin-").replace("-", "_")] = argv.pop(0)
        elif option == "--limit" and argv:
            lead += [option, argv.pop(0)]
        elif option in SHARED_FLAGS:
            lead.append(option)
        else:
            print(_usage(), file=sys.stderr)
            return 2
    if not argv:
        print(_usage(), file=sys.stderr)
        return 2
    name, rest = argv[0], argv[1:]
    if name in INTERNAL:
        return _internal(name, rest)
    if name not in COMMANDS:
        print(f"ctx: unknown command {name!r}\n{_usage()}", file=sys.stderr)
        return 2
    module = importlib.import_module(COMMANDS[name])
    parser = _parser(name)
    module.add_arguments(parser)
    args = parser.parse_args([*lead, *rest])
    args.plugin_root, args.plugin_data = launcher.get("root"), launcher.get("data")
    started = time.monotonic()

    from wscontext.cli.output import error
    from wscontext.config import load_config
    from wscontext.gitutil import GitError
    from wscontext.store.paths import StorePaths
    try:
        store = StorePaths.resolve(Path.cwd())
    except (GitError, OSError):
        reply = error("not inside a Git repository", args.json)
        store = None
    else:
        config = load_config(store.root)
        if name in FETCHING and config.sync.remote:
            _spawn(store, ["fetch", "ctx", "", str(store.root)])
        if name in FETCHING:
            from wscontext.capture.state import sweep_due
            if sweep_due(store, config):
                _spawn(store, ["sweep", "", "", str(store.root)])
        if name in MIGRATING:
            _migrate(store)
        reply = module.run(args, store, config)
    # Errors go to stderr, except as JSON, which a caller parses from stdout.
    print(reply.text, file=sys.stderr if reply.code and not (args.json or reply.report) else sys.stdout)
    if store is not None:
        _log_call(store, config, [name, *rest], reply, started)
    return reply.code


if __name__ == "__main__":
    sys.exit(main())
