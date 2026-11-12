"""Recover checkpoint records for commits that never had one, from the stored sessions.

    .venv/bin/python acceptance/recover_checkpoints.py REPO --map FILE [--map FILE ...] [--dry-run]

A one-off repair. Takes every commit on the branch (default `master`) with no checkpoint record and
looks through the stored sessions for a Bash call that ran `git commit` whose result names the
commit's SHA, or its old SHA from a map (lines of `old new`, as the identity rewrite left them), as
a prefix of 7 characters or more. A subagent transcript counts for its parent session. One session
found: the commit is linked to it. Several: to the one whose matching result came first, and the
dry run marks it. None: the commit is listed and left alone.

Each linked commit gets a checkpoint record through the normal record writer, and all of them go
into one commit on `refs/wscontext/history`. `--dry-run` prints the plan and writes nothing. A real
run first copies `records/checkpoints/` and the history ref's old value to `--backup DIR` (default:
a new folder under the store's `state/`), then rebuilds the search index.
"""

import argparse
import re
import shlex
import shutil
import sys
import time
from pathlib import Path

from record_corpus import RAW_NAME
from wscontext.adapters import claude_code
from wscontext.adapters.claude_code import ClaudeCodeAdapter
from wscontext.config import load_config
from wscontext.githooks.handlers import commit_links
from wscontext.githooks.worker import _diff_stats
from wscontext.gitutil import git
from wscontext.history.branch import HISTORY_REF, commit_paths, read_tip
from wscontext.index import build as index_build
from wscontext.redact.account import with_account_ids
from wscontext.store.locks import FileLock
from wscontext.store.paths import StorePaths
from wscontext.store.records import CheckpointRecord, checkpoint_path, write_checkpoint

GIT_COMMIT = re.compile(r"\bgit\b[^\n|;&]*\bcommit\b")
# a Bash call's summary is `command=..., description=..., timeout=...`; only the command counts
BASH_COMMAND = re.compile(
    r"command=(.*?)(?:, (?:description|timeout|run_in_background|dangerouslyDisableSandbox)=.*)?", re.S)
HEX = re.compile(r"\b[0-9a-f]{7,64}\b")


def read_maps(maps: list[Path]) -> dict[str, str]:
    """New SHA -> old SHA."""
    old_of: dict[str, str] = {}
    for path in maps:
        for line in path.expanduser().read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) >= 2:
                old_of.setdefault(fields[1], fields[0])
    return old_of


def transcripts(store: StorePaths) -> list[tuple[str, Path, bool]]:
    """Every stored transcript as (session ID, file, is a subagent file). Snapshots are skipped."""
    found = []
    for path in sorted((store.raw / "sessions").glob("*.jsonl")):
        m = RAW_NAME.fullmatch(path.name)
        if m and not m["pc"]:
            found.append((m["sid"], path, False))
    for path in sorted((store.raw / "subagents").glob("*/*.jsonl")):
        found.append((path.parent.name, path, True))
    return found


def commit_results(store: StorePaths) -> tuple[list[tuple[str, str, str]], int]:
    """(session ID, time, result text) of every Bash `git commit` call's result, and the number
    of transcripts the adapter does not support."""
    adapter = ClaudeCodeAdapter()
    results = []
    unsupported = 0
    # The adapter shortens a command to 80 characters and a result to 2000, for display.
    # A `git commit` after a long `git add` and a SHA late in the output need the whole text.
    saved = claude_code.INPUT_VALUE_LIMIT, claude_code.RESULT_LIMIT
    claude_code.INPUT_VALUE_LIMIT = claude_code.RESULT_LIMIT = sys.maxsize
    try:
        parsed_all = [(sid, adapter.parse(path, sidechain=sidechain))
                      for sid, path, sidechain in transcripts(store)]
    finally:
        claude_code.INPUT_VALUE_LIMIT, claude_code.RESULT_LIMIT = saved
    for sid, parsed in parsed_all:
        if not parsed.supported:
            unsupported += 1
            continue
        for turn in parsed.turns:
            commands = set()
            for i in turn.items:
                m = BASH_COMMAND.fullmatch(i.text) if i.kind == "tool_call" and i.name == "Bash" else None
                if m and GIT_COMMIT.search(m[1]):
                    commands.add(i.tool_id)
            results += [(sid, turn.started_at or "", i.text) for i in turn.items
                        if i.kind == "tool_result" and i.tool_id in commands]
    return results, unsupported


def named(text: str, *shas: str) -> bool:
    return any(sha.startswith(token) for token in HEX.findall(text) for sha in shas)


def plan_links(store: StorePaths, branch: str, old_of: dict[str, str]):
    """Returns (links, unmatched, unsupported). A link is (sha, old sha or None, session ID,
    whether several sessions matched)."""
    commits = git("rev-list", branch, cwd=store.root).split()
    missing = [c for c in commits if not checkpoint_path(store, c).exists()]
    results, unsupported = commit_results(store)
    links, unmatched = [], []
    for sha in missing:
        old = old_of.get(sha)
        first: dict[str, str] = {}  # session ID -> time of its earliest matching result
        for sid, when, text in results:
            if named(text, *(s for s in (sha, old) if s)):
                first[sid] = min(when, first.get(sid, when))
        if first:
            sid = min(first, key=lambda s: (first[s], s))
            links.append((sha, old, sid, len(first) > 1))
        else:
            unmatched.append(sha)
    return links, unmatched, unsupported


def subjects(store: StorePaths, shas: list[str]) -> dict[str, tuple[str, str]]:
    """SHA -> (committer date, subject)."""
    out = git("log", "--no-walk=unsorted", "--stdin", "--format=%H%x09%cI%x09%s",
              cwd=store.root, input="".join(f"{s}\n" for s in shas))
    return {f[0]: (f[1], f[2]) for f in (line.split("\t", 2) for line in out.splitlines())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("repo", type=Path)
    parser.add_argument("--map", type=Path, action="append", default=[], metavar="FILE",
                        help="old SHA, new SHA per line; repeatable")
    parser.add_argument("--branch", default="master")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    parser.add_argument("--backup", type=Path, metavar="DIR",
                        help="where a real run saves what it replaces (default: under the store's state/)")
    args = parser.parse_args(argv)
    store = StorePaths.resolve(args.repo)
    links, unmatched, unsupported = plan_links(store, args.branch, read_maps(args.map))
    info = subjects(store, [link[0] for link in links] + unmatched)
    print(f"{len(links)} commit(s) to link")
    for sha, old, sid, several in links:
        mark = "  [earlier result of several sessions]" if several else ""
        print(f"  {sha[:7]} -> {sid[:8]}{mark}  {info[sha][1][:60]}")
    print(f"{len(unmatched)} commit(s) unmatched, left alone")
    for sha in unmatched:
        print(f"  {sha[:7]}  {info[sha][1][:60]}")
    if unsupported:
        print(f"{unsupported} transcript(s) in an unsupported format were not searched")
    if args.dry_run:
        print("\ndry run: nothing written")
        return 0
    if not links:
        print("\nnothing to do")
        return 0
    config = with_account_ids(load_config(store.root), store)
    backup = (args.backup or store.state / "backups" / f"recover-checkpoints-{time.strftime('%Y%m%dT%H%M%S')}")
    backup = backup.expanduser().resolve()
    with FileLock(store.state / "locks" / "githooks.lock", stale_after=300, timeout=120):
        backup.mkdir(parents=True, exist_ok=True)
        if (store.records / "checkpoints").is_dir():
            shutil.copytree(store.records / "checkpoints", backup / "checkpoints")
        (backup / "history-ref.txt").write_text(f"{read_tip(store) or 'none'}\n", encoding="utf-8")
        authors = commit_links(store, [link[0] for link in links])
        written = []
        for sha, old, sid, _ in links:
            files, insertions, deletions = _diff_stats(store, sha)
            written.append(write_checkpoint(store, CheckpointRecord(
                sha=sha, author=authors[sha][0], session_ids=[sid], created_at=info[sha][0],
                files_changed=files, insertions=insertions, deletions=deletions,
                rewritten_from=old), config))
        tip = commit_paths(store, written, f"wscontext: {len(written)} recovered checkpoints")
    print(f"\nwrote {len(written)} checkpoint record(s); {HISTORY_REF} at {(tip or 'none')[:12]}")
    print(f"backup: {backup}\nrestore: git update-ref {HISTORY_REF} <value in history-ref.txt> "
          f"and copy {shlex.quote(str(backup / 'checkpoints'))} back to {store.records / 'checkpoints'}")
    result = index_build.rebuild(store)
    print("index: " + ("locked by another process; run ctx rebuild" if result.get("locked")
                       else f"rebuilt, {result['sessions_indexed']} sessions, "
                            f"{result['commits_indexed']} commits"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
