"""`ctx brief`: the brief a session starting now would get, or why there is none."""

import json

from wscontext.capture import brief
from wscontext.cli.output import Reply, estimate_tokens
from wscontext.config import Config
from wscontext.index import build
from wscontext.store.paths import StorePaths


def add_arguments(parser) -> None:
    pass


def run(args, store: StorePaths, config: Config) -> Reply:
    reason = None
    text = None
    if not config.retrieval.enabled:
        reason = "retrieval is switched off ([retrieval] enabled = false)"
    elif not config.retrieval.brief:
        reason = "the brief is switched off ([retrieval] brief = false)"
    else:
        build.refresh(store)
        text = brief.build(store, config, "")
        if text is None:
            reason = "no relevant prior work on this branch or in the files you have changed"
    if args.json:
        return Reply(json.dumps({"command": "ctx brief", "brief": text, "reason": reason,
                                 "tokens": estimate_tokens(text) if text else 0}, indent=2,
                                ensure_ascii=False), results=int(text is not None))
    return Reply(text if text is not None else f"No brief: {reason}.", results=int(text is not None))
