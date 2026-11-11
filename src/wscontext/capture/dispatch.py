"""`ctx hook <event>`: read the hook input, run the handler within the time budget, exit 0."""

import json
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from wscontext.capture.handlers import HANDLERS
from wscontext.config import Config, load_config
from wscontext.redact.account import with_account_ids
from wscontext.logs import HOOKS_LOG, append_log, now_iso
from wscontext.store.paths import StorePaths, is_safe_name


def run_budgeted(event: str, work: Callable[[dict, dict], None]) -> dict:
    """Run `work(entry, ctx)` to completion or to its time budget; returns the hook-run log entry.

    `work` puts `store` and `config` into `ctx` as soon as it knows them, so the
    configured budget applies and the entry is logged. It may put hook output into
    `ctx["output"]`; the entry carries it as `output` only when `work` finished inside the
    budget. Never raises.
    """
    started = time.monotonic()
    entry: dict = {"ts": now_iso(), "event": event, "ok": True}
    ctx: dict = {}

    def guarded() -> None:
        try:
            work(entry, ctx)
        except BaseException as e:
            entry["ok"] = False
            entry["error"] = f"{type(e).__name__}: {e}"

    # The budget is configured per repository: the default applies until the config is loaded.
    thread = threading.Thread(target=guarded, daemon=True)
    thread.start()
    while thread.is_alive():
        budget = (ctx["config"] if "config" in ctx else Config()).hooks.time_budget_ms
        if time.monotonic() - started >= budget / 1000:
            entry["ok"] = False
            entry["error"] = f"timeout after {budget} ms"
            break
        thread.join(0.005)
    entry["ms"] = round((time.monotonic() - started) * 1000)
    if "store" in ctx:
        try:
            append_log(ctx["store"], ctx["config"], HOOKS_LOG, dict(entry))
        except BaseException:
            pass
    if entry["ok"] and "output" in ctx:
        entry["output"] = ctx["output"]  # after the log write: the log never holds it
    return entry


def run(event: str, raw: str) -> dict:
    """Run one Claude Code hook; returns the hook-run log entry. Never raises."""

    def work(entry: dict, ctx: dict) -> None:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("hook input is not a JSON object")
        sid = payload.get("session_id")
        entry["session_id"] = sid
        handler = HANDLERS.get(event.lower().replace("-", "").replace("_", ""))
        if handler is None:
            raise ValueError(f"unknown hook event {event!r}")
        if not isinstance(sid, str) or not is_safe_name(sid):
            raise ValueError("missing or unsafe session_id")
        store = StorePaths.resolve(Path(payload.get("cwd") or "."))
        ctx["store"] = store
        ctx["config"] = with_account_ids(load_config(store.root), store)
        out = handler(store, ctx["config"], payload)
        if isinstance(out, str) and out:
            ctx["output"] = json.dumps({"hookSpecificOutput": {
                "hookEventName": "SessionStart", "additionalContext": out}})

    return run_budgeted(event, work)


def main(argv: list[str]) -> int:
    try:
        entry = run(argv[0] if argv else "", sys.stdin.read())
        if entry.get("output"):
            sys.stdout.write(entry["output"] + "\n")
    except BaseException:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
