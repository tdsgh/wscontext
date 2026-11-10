"""The detached graph worker: a full graph refresh in its own process.

Run as `python -m wscontext.graph.worker refresh <repo-root>`. A query whose budgeted refresh ran
out of time starts it, and so do the Git hooks after a branch switch or a merge. It never waits
for the graph lock: when another writer holds it, the worker logs `skipped` and leaves the graph
to that writer.
"""

import sys
import time
from pathlib import Path

from wscontext.config import load_config
from wscontext.graph import build
from wscontext.graph.languages import load_parts
from wscontext.logs import HOOKS_LOG, append_log, now_iso
from wscontext.store.paths import StorePaths


def run(action: str, root: str) -> None:
    store = StorePaths.resolve(Path(root))
    config = load_config(store.root)
    started = time.monotonic()
    entry: dict = {"ts": now_iso(), "event": "worker:graph", "ok": True}
    try:
        if action != "refresh":
            raise ValueError(f"unknown worker action {action!r}")
        parts, unavailable = load_parts(config.graph.languages)
        if unavailable:
            entry["unavailable"] = unavailable
        # this worker answers the refresh that started it, whether or not it gets the lock
        build.claim_path(store).unlink(missing_ok=True)
        if not parts:
            entry["skipped"] = "no language part available"
        else:
            result = build.refresh(store, config)
            if result.busy:
                entry["skipped"] = "another writer holds the graph lock"
            else:
                entry.update(parsed=result.parsed, parse_errors=len(result.parse_errors),
                             pending_files=result.pending_files,
                             pending_history=result.pending_history)
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
        run(*argv[:2])
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
