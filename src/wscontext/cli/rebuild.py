"""`ctx rebuild`: delete the search index and build it again from captured history."""

import json

from wscontext.cli.output import Reply, error
from wscontext.config import Config
from wscontext.index import build
from wscontext.store.paths import StorePaths


def add_arguments(parser) -> None:
    pass


def run(args, store: StorePaths, config: Config) -> Reply:
    result = build.rebuild(store)
    if result["locked"]:
        return error("another process is updating the index; try again shortly", args.json)
    if args.json:
        return Reply(json.dumps({"command": "ctx rebuild", **result}, indent=2))
    lines = [f"Index rebuilt: {result['sessions_indexed']} sessions, "
             f"{result['commits_indexed']} commits indexed."]
    if result["pending_commits"]:
        lines.append(f"{result['pending_commits']} commits are not in this clone yet and "
                     "will be indexed once they are.")
    return Reply("\n".join(lines))
