"""Capture health: hook and worker runs, failures and latency per event."""

from datetime import datetime

from wscontext.logs import HOOKS_LOG
from wscontext.stats.data import percentile, read_log
from wscontext.store.paths import StorePaths

ERRORS_SHOWN = 5


def summarize(store: StorePaths, since: datetime | None) -> dict:
    runs = read_log(store, HOOKS_LOG, since)
    by_event: dict[str, list[dict]] = {}
    for row in runs:
        by_event.setdefault(str(row.get("event", "unknown")), []).append(row)
    events = []
    for event, rows in sorted(by_event.items()):
        times = [r["ms"] for r in rows if isinstance(r.get("ms"), (int, float))]
        events.append({"event": event, "runs": len(rows),
                       "failures": sum(r.get("ok") is False for r in rows),
                       "p50_ms": percentile(times, 50), "p95_ms": percentile(times, 95)})
    failed = [r for r in runs if r.get("ok") is False]
    return {"runs": len(runs), "failures": len(failed), "events": events,
            "recent_failures": [{"ts": r.get("ts"), "event": r.get("event"),
                                 "session_id": r.get("session_id"), "error": r.get("error")}
                                for r in failed[-ERRORS_SHOWN:]]}
