"""`ctx git-hook <hook> [<git args>...]`: run a Git hook's handler within the time budget, exit 0.

Run as `python -m wscontext.githooks.dispatch <hook> [<git args>...]` from the hook's
working directory; `post-rewrite` and `pre-push` read the hook input on stdin.
"""

import sys
from pathlib import Path

from wscontext.capture.dispatch import run_budgeted
from wscontext.config import load_config
from wscontext.githooks.handlers import HANDLERS
from wscontext.githooks.install import STDIN_HOOKS
from wscontext.store.paths import StorePaths


def run(hook: str, args: list[str], stdin: str, cwd: Path | None = None) -> dict:
    """Run one Git hook; returns the hook-run log entry. Never raises."""

    def work(entry: dict, ctx: dict) -> None:
        store = StorePaths.resolve(cwd or Path.cwd())
        ctx["store"] = store
        ctx["config"] = load_config(store.root)
        handler = HANDLERS.get(hook)
        if handler is None:
            raise ValueError(f"unknown git hook {hook!r}")
        handler(store, ctx["config"], args, stdin)

    return run_budgeted(f"git:{hook}", work)


def main(argv: list[str]) -> int:
    try:
        hook = argv[0] if argv else ""
        run(hook, argv[1:], sys.stdin.read() if hook in STDIN_HOOKS else "")
    except BaseException:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
