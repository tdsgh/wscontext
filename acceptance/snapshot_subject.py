"""Cut a benchmark subject from a repository: a bundle at one commit and the store as it stood then.

    .venv/bin/python acceptance/snapshot_subject.py REPO --commit SHA --before-session ID --out DIR [--branch master]

Writes, into a new or empty DIR:

- `repo.bundle`: the branch at SHA, with no other ref.
- `store/`: the session records and raw files (transcript copies, PreCompact snapshots, subagent
  files) of every session that started before session ID started and has no stored entry, its own
  or a subagent's, at or after that moment, and the checkpoint records whose commit is reachable
  from SHA. Copied as stored; they passed the redactor when captured. A kept checkpoint names only
  kept sessions; one left with none is left out. A session that started before the cut and has an
  entry at or after it stops the snapshot with an error naming it. `ended_at` plays no part.
- `subject.toml`, for `benchmark.py --subject DIR`.

Only reads REPO and its store. Prints the session and checkpoint counts kept and left out. Not part
of the offline suite (pytest never collects `acceptance/`).
"""

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from record_corpus import RAW_NAME
from recover_checkpoints import transcripts
from wscontext.adapters.claude_code import ClaudeCodeAdapter
from wscontext.gitutil import GitError, git
from wscontext.store.paths import StorePaths

SUBJECT_TOML = """\
bundle = "repo.bundle"
branch = "{branch}"
store = "store"
suite = ".venv/bin/pytest -q -x"
setup = "uv-venv"
tools = "Bash(.venv/bin/pytest:*)"
"""


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def when(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def last_entries(store: StorePaths) -> dict[str, datetime]:
    """Session ID -> its latest stored entry, from its own transcript and its subagents', read through the adapter."""
    adapter, last = ClaudeCodeAdapter(), {}
    for sid, path, sidechain in transcripts(store):
        parsed = adapter.parse(path, sidechain=sidechain)
        if not parsed.supported:
            sys.exit(f"{path}: a transcript the adapter does not support, so its last entry is unknown")
        for e in parsed.events:
            if e.timestamp:
                last[sid] = max(last.get(sid, when(e.timestamp)), when(e.timestamp))
    return last


def started_before(store: StorePaths, prefix: str) -> tuple[set[str], set[str]]:
    """(session IDs that started before session `prefix` and have no stored entry at or after that
    moment, the other session IDs). A session that spans the cut stops the snapshot."""
    records = {p.stem: read_json(p) for p in sorted((store.records / "sessions").glob("*.json"))}
    cut = [sid for sid in records if sid.startswith(prefix)]
    if len(cut) != 1:
        sys.exit(f"--before-session {prefix}: {len(cut)} stored sessions match, wanted 1")
    if not records[cut[0]].get("started_at"):
        sys.exit(f"session {cut[0]} has no start time")
    start = when(records[cut[0]]["started_at"])
    earlier = {sid for sid, r in records.items() if sid != cut[0] and r.get("started_at") and when(r["started_at"]) < start}
    last = last_entries(store)
    spanning = sorted(sid for sid in earlier if sid in last and last[sid] >= start)
    if spanning:
        sys.exit(f"session(s) {', '.join(spanning)} started before {cut[0]} and have an entry at or after its start "
                 f"({start.isoformat()}); the snapshot cannot cut between them")
    return earlier, set(records) - earlier


def copy_sessions(store: StorePaths, kept: set[str], out: Path) -> int:
    """Copies the records and raw files of the kept sessions; returns the number of raw files."""
    files = 0
    for sid in sorted(kept):
        (out / "records" / "sessions").mkdir(parents=True, exist_ok=True)
        shutil.copy2(store.records / "sessions" / f"{sid}.json", out / "records" / "sessions" / f"{sid}.json")
    for path in sorted((store.raw / "sessions").glob("*.jsonl")):
        m = RAW_NAME.fullmatch(path.name)
        if m and m["sid"] in kept:
            (out / "raw" / "sessions").mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, out / "raw" / "sessions" / path.name)
            files += 1
    for folder in sorted((store.raw / "subagents").glob("*")):
        if folder.name in kept:
            shutil.copytree(folder, out / "raw" / "subagents" / folder.name)
            files += sum(1 for p in folder.rglob("*") if p.is_file())
    # the runner copies raw/ and records/ whole
    (out / "raw").mkdir(parents=True, exist_ok=True)
    (out / "records" / "sessions").mkdir(parents=True, exist_ok=True)
    return files


def copy_checkpoints(store: StorePaths, reachable: set[str], kept: set[str], out: Path) -> tuple[int, int]:
    """Returns (checkpoints kept, checkpoints left out)."""
    (out / "records" / "checkpoints").mkdir(parents=True, exist_ok=True)
    taken = left = 0
    for path in sorted((store.records / "checkpoints").glob("*.json")):
        rec = read_json(path)
        ids = [s for s in rec["session_ids"] if s in kept]
        if rec["sha"] not in reachable or not ids:
            left += 1
            continue
        target = out / "records" / "checkpoints" / path.name
        if ids == rec["session_ids"]:
            shutil.copy2(path, target)
        else:
            rec["session_ids"] = ids
            target.write_text(json.dumps(rec, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        taken += 1
    return taken, left


def make_bundle(repo: Path, sha: str, branch: str, target: Path) -> None:
    """A bundle holding only `branch` at `sha`, cut in a scratch clone so the repository's refs stay as they are."""
    with tempfile.TemporaryDirectory(prefix="wscontext-snapshot-") as tmp:
        scratch = Path(tmp) / "scratch.git"
        git("clone", "-q", "--bare", "--no-tags", "--single-branch", "--branch", branch, str(repo), str(scratch), cwd=Path(tmp))
        git("update-ref", f"refs/heads/{branch}", sha, cwd=scratch)
        git("bundle", "create", "-q", str(target.resolve()), f"refs/heads/{branch}", cwd=scratch)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("repo", type=Path)
    parser.add_argument("--commit", required=True, help="the commit the branch is cut at")
    parser.add_argument("--before-session", required=True, metavar="ID",
                        help="keep the sessions that started before this one and have no entry at or after its start (an ID prefix works)")
    parser.add_argument("--out", type=Path, required=True, help="a new or empty folder")
    parser.add_argument("--branch", default="master")
    args = parser.parse_args(argv)
    if args.out.exists() and any(args.out.iterdir()):
        sys.exit(f"{args.out} is not empty")
    store = StorePaths.resolve(args.repo)
    try:
        sha = git("rev-parse", "--verify", f"{args.commit}^{{commit}}", cwd=store.root).strip()
        git("merge-base", "--is-ancestor", sha, args.branch, cwd=store.root)
    except GitError as e:
        sys.exit(f"{args.commit} is not a commit on {args.branch}: {e}")
    reachable = set(git("rev-list", sha, cwd=store.root).split())
    kept, left = started_before(store, args.before_session)

    args.out.mkdir(parents=True, exist_ok=True)
    make_bundle(store.root, sha, args.branch, args.out / "repo.bundle")
    raw_files = copy_sessions(store, kept, args.out / "store")
    taken, dropped = copy_checkpoints(store, reachable, kept, args.out / "store")
    (args.out / "subject.toml").write_text(SUBJECT_TOML.format(branch=args.branch), encoding="utf-8")
    print(f"snapshot of {args.branch} at {sha[:12]} in {args.out}\n"
          f"  sessions:    {len(kept)} kept ({raw_files} raw files), {len(left)} left out\n"
          f"  checkpoints: {taken} kept, {dropped} left out")
    return 0


if __name__ == "__main__":
    sys.exit(main())
