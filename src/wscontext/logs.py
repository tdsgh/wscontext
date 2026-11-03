"""Host-local JSONL logs under `<store>/logs/`, rotated by size. Stdlib only; safe in a hook."""

import json
import os
from datetime import datetime, timezone

from wscontext.config import Config
from wscontext.redact.redactor import redact_text
from wscontext.store.paths import StorePaths

HOOKS_LOG = "hooks.jsonl"
CTX_CALLS_LOG = "ctx-calls.jsonl"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _scrub(value, config: Config):
    if isinstance(value, str):
        return redact_text(value, config)[0]
    if isinstance(value, dict):
        return {k: _scrub(v, config) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v, config) for v in value]
    return value


def append_log(store: StorePaths, config: Config, name: str, entry: dict) -> None:
    """Append one redacted JSON line to `logs/<name>`; keeps one rotated generation, `<name>.1`."""
    line = json.dumps(_scrub(entry, config), ensure_ascii=False, sort_keys=True) + "\n"
    path = store.logs / name
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size >= config.logs.max_bytes:
            os.replace(path, path.with_name(name + ".1"))
    except OSError:
        pass
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
