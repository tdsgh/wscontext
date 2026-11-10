"""Record the stage-2 eval corpus: a scripted story played by headless sessions in a throwaway repository.

    .venv/bin/python acceptance/record_corpus.py [--dry-run | --check] [--force] [--keep] [--limit N]
    .venv/bin/python acceptance/record_corpus.py --resume DIR [--from TASK_ID] [--dry-run] [--limit N]
    .venv/bin/python acceptance/record_corpus.py --retrim SOURCE_DIR

Plays eval/stage2/tasks.toml, one `claude -p` session per task, in a throwaway repository under a
temporary directory with a fixed Git identity, each task committed. It then copies into
eval/stage2/corpus/ the store's `raw/` and `records/` files of the accepted sessions and the story's
commits, and saves the story's branches as eval/stage2/repo.bundle, so `ctx eval` can index the
sessions and the commit messages. It fails, writing nothing, when the corpus or the bundle holds
the home directory path, the operator's email or an account identifier key (`accountUuid`, `organizationUuid`).

Spends API usage once (about 25 sessions); `--dry-run` validates the story and prints the plan
without a call, and `--check` scans an already recorded corpus and bundle without a call. The
recorded corpus is meant to be committed once and left alone: `--force` replaces it.

The sessions are isolated from the operator's user-level Claude Code configuration (settings,
CLAUDE.md and rules, auto memory, MCP servers, other plugins): only the wscontext plugin is
enabled, so the corpus holds no personal rules and no private data. The privacy scan runs over
the store after every task, so a leak stops the run at once.

A task fails when its session makes no commit with the task's message, claude reports an error,
or claude times out. The run records the failure, commits the working tree with the task's
message itself when the session made no commit, and goes on, so one run shows every task that
needs a fix. When any task failed it writes no corpus, keeps the working directory, lists the
failures and prints the resume command. Progress is kept in `<working directory>/recording.json`
after every task. A leak in the store, or a hook that never finishes a session, stops the run.

Resume after a failure, once tasks.toml is fixed (the tasks before the resume point must keep
their ids and order):

    .venv/bin/python acceptance/record_corpus.py --resume DIR

It rewinds DIR to just before the first failed task: branch heads, working tree, and the store
files of that task's session and every later one. It then plays the rest of the story from
tasks.toml. `--from TASK_ID` rewinds to another recorded task, or starts the next unplayed one.
With `--dry-run` it prints what would be rewound and replayed. `--limit N` plays only the first
N tasks of the story, on a full run or a resume.

Re-trim the recorded corpus after a change to the reduction list or the redactor, offline:

    .venv/bin/python acceptance/record_corpus.py --retrim ~/.claude/projects/<recording project>

SOURCE_DIR holds the recording's source transcripts, read only. Each transcript copy is stored again
as a capture with the current code would store it, and each record's stored sizes follow; file
names, the other record fields, the checkpoints and repo.bundle stay. It fails, writing nothing,
when a source changed after the capture or a copy parses differently afterwards.

A one-task trial, a few cents, into a scratch directory:

    .venv/bin/python acceptance/record_corpus.py --limit 1 --out /tmp/corpus-trial --force --keep
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from stage1 import COMMIT_SCRIPT, PLUGIN_ID, TOOLS, Acc, Fail, check, locate_plugin
from wscontext.adapters.claude_code import ClaudeCodeAdapter
from wscontext.capture.worker import copy_reduced, stored_sizes
from wscontext.config import load_config
from wscontext.store.paths import is_safe_name
from wscontext.store.records import SessionRecord, read_record, write_record

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "eval" / "stage2"
IDENTITY = ("Corpus Recorder", "corpus@example.invalid")
KINDS = {"feature", "rejected", "bugfix", "refactor"}
# the stories run their own code to measure and test it, look around, and use ctx as the
# CLAUDE.md block ctx init writes tells them to; a compound command needs every part allowed
STORY_TOOLS = TOOLS + "".join(f" Bash({c}:*)" for c in (
    "python3", "ctx", "ls", "cat", "head", "tail", "grep", "find", "pwd", "wc",
    "git log", "git diff", "git show", "git rm", "git mv"))
# what the sessions are told about the run, so a refused command or a doubt does not end in a question
UNATTENDED = ("This is an unattended recording: nobody will answer questions or grant permissions. "
              "Run every command from the repository root, without cd or environment variable prefixes; "
              "remove or move tracked files with git rm and git mv. Plain rm, mkdir and cd are refused; "
              "the Write tool creates directories.")
REPO = "tally"
STATE = "recording.json"
REPLY_CHARS = 600
HISTORY_REF = "refs/wscontext/history"
# a transcript copy in raw/sessions/: <date>-<session>.jsonl, or a pre-compact snapshot <date>-pc<stamp>-<session>.jsonl
RAW_NAME = re.compile(r"\d{4}-\d\d-\d\d-(?P<pc>pc\d{8}T\d{6}Z-)?(?P<sid>.+)\.jsonl")
ACCOUNT_KEYS = ("accountUuid", "organizationUuid")


def isolation(plugin_id: str) -> tuple[str, ...]:
    """claude flags that cut a session off from the operator's user-level configuration.

    --setting-sources drops user settings; claudeMdExcludes drops ~/.claude/CLAUDE.md, ~/.claude/rules
    and anything else under the home directory; --strict-mcp-config with no --mcp-config loads no
    MCP server; --settings re-enables only the wscontext plugin (the installed copy, so there is no
    second --plugin-dir copy to capture twice) and clears the commit/PR attribution."""
    home = {str(Path.home()), str(Path.home().resolve())}
    settings = {"enabledPlugins": {plugin_id: True},
                "claudeMdExcludes": sorted(f"{h}/**" for h in home),
                "attribution": {"commit": "", "pr": ""}}
    return ("--setting-sources", "project,local", "--strict-mcp-config", "--settings", json.dumps(settings))


@dataclass
class Task:
    id: str
    kind: str
    prompt: str
    message: str
    branch: str = "main"
    merge: bool = False


def load_tasks(path: Path) -> list[Task]:
    tasks = []
    for raw in tomllib.loads(path.read_text(encoding="utf-8")).get("task", []):
        unknown = set(raw) - {"id", "kind", "prompt", "message", "branch", "merge"}
        check(not unknown, f"{path.name}: task {raw.get('id')!r} has unknown fields {sorted(unknown)}")
        try:
            tasks.append(Task(**raw))
        except TypeError as e:
            raise Fail(f"{path.name}: task {raw.get('id')!r}: {e}")
    check(tasks, f"{path.name} holds no tasks")
    ids = [t.id for t in tasks]
    check(len(set(ids)) == len(ids), f"{path.name}: duplicate task ids")
    for t in tasks:
        check(t.kind in KINDS, f"{path.name}: task {t.id!r} has kind {t.kind!r}, wanted one of {sorted(KINDS)}")
        check('"' not in t.message and "$" not in t.message and "`" not in t.message,
              f"{path.name}: task {t.id!r}: the message goes through a shell command; no quotes, $ or backticks")
        check(not t.merge or t.branch != "main", f"{path.name}: task {t.id!r} merges main into itself")
    return tasks


def forbidden() -> dict[str, str]:
    """What must not appear in the recorded files, by name: the home directory, the operator's
    email, and the keys Claude Code writes account identifiers under, whatever their values."""
    found = {"home directory": str(Path.home()), "resolved home directory": str(Path.home().resolve())}
    done = subprocess.run(["git", "config", "--global", "user.email"], capture_output=True, text=True)
    if done.stdout.strip():
        found["operator email"] = done.stdout.strip()
    return found | {f"account identifier ({key})": key for key in ACCOUNT_KEYS}


def scan_files(root: Path, banned: dict[str, str]) -> list[str]:
    """Where a banned string appears in a file under root."""
    problems = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        data = path.read_bytes()
        problems += [f"{name} in {path.relative_to(root)}" for name, value in banned.items() if value.encode() in data]
    return problems


def scan(root: Path, bundle: Path, banned: dict[str, str]) -> list[str]:
    """Where a banned string appears: in a corpus file, or in the bundle's history, patches included."""
    problems = scan_files(root, banned)
    with tempfile.TemporaryDirectory(prefix="wscontext-bundle-") as tmp:
        clone = Path(tmp) / "clone"
        subprocess.run(["git", "clone", "-q", "--mirror", str(bundle), str(clone)], check=True,
                       capture_output=True, env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull})
        log = subprocess.run(["git", "--git-dir", str(clone), "log", "--all", "-p", "--format=%an %ae %cn %ce%n%B"],
                             capture_output=True, text=True, check=True).stdout
    problems += [f"{name} in the bundle's history" for name, value in banned.items() if value in log]
    return problems


def check_recorded(out: Path) -> None:
    banned = forbidden()
    bundle = out / "repo.bundle"
    check(bundle.is_file() and (out / "corpus" / "records").is_dir(), f"no recorded corpus and bundle in {out}")
    found = scan(out / "corpus", bundle, banned)
    check(not found, "the recorded files hold private data: " + "; ".join(found))
    print(f"clean: no {', '.join(banned)} in {out / 'corpus'} or {bundle}")


# --- the recording's state file ------------------------------------------------------------------

def save(work: Path, record: dict) -> None:
    """Write recording.json whole, through a rename, so an interrupted run leaves the last state."""
    tmp = work / f".{STATE}.tmp"
    tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, work / STATE)


def load_record(work: Path) -> dict:
    try:
        record = json.loads((work / STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise Fail(f"{work} holds no readable {STATE}: {e}")
    check(record.get("version") == 1, f"{work / STATE}: unknown version {record.get('version')!r}")
    check((work / REPO / ".git").is_dir(), f"{work} holds no {REPO} repository")
    return record


def heads(acc: Acc, repo: Path) -> dict[str, str]:
    out = acc.git(repo, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads/")
    return dict(line.split(" ", 1) for line in out.splitlines())


def failures(record: dict) -> list[tuple[int, dict]]:
    return [(n, e) for n, e in enumerate(record["tasks"], 1) if e["outcome"] != "ok"]


def report_failures(record: dict) -> None:
    failed = failures(record)
    print(f"\n{len(failed)} of {len(record['tasks'])} played tasks failed:")
    for n, e in failed:
        print(f"  {n:2}. {e['id']} (session {e.get('session')}): {e.get('reason')}")
        if e.get("tools"):
            print(f"      tools used: {', '.join(e['tools'])}")
        if e.get("reply"):
            print(f"      final reply: {' '.join(e['reply'].split())}")


# --- playing the story ---------------------------------------------------------------------------

def setup_repo(acc: Acc) -> Path:
    repo = acc.tmp / REPO
    repo.mkdir()
    acc.git(repo, "init", "-q", "-b", "main")
    acc.git(repo, "config", "user.name", IDENTITY[0])
    acc.git(repo, "config", "user.email", IDENTITY[1])
    (repo / "README.md").write_text("# tally\n")
    (repo / ".gitignore").write_text("__pycache__/\n")
    acc.git(repo, "add", "README.md", ".gitignore")
    acc.git(repo, "commit", "-q", "-m", "Start the tally project")
    acc.ctx(repo, "init")
    return repo


def is_active(acc: Acc, repo: Path, sid: str) -> bool:
    try:
        active = json.loads((acc.store(repo) / "state" / "active.json").read_bytes())
    except (OSError, ValueError):
        return False
    return isinstance(active, dict) and any(isinstance(s, dict) and sid in s for s in active.values())


def end_session(acc: Acc, repo: Path, sid: str) -> str:
    """End a failed task's session, so its trailer does not land on the next tasks' commits.

    A killed claude never ran SessionEnd; the hook is run for it. Returns a note when the
    session's record did not end."""
    if is_active(acc, repo, sid):
        acc.hook(repo, "SessionEnd", sid)
    if not (acc.store(repo) / "records" / "sessions" / f"{sid}.json").exists():
        return ""
    try:
        acc.wait_ended(repo, sid)
    except Fail as e:
        return f"; {e}"
    return ""


def play(acc: Acc, repo: Path, story: list[Task], banned: dict[str, str], record: dict) -> None:
    """Play the story's tasks after the ones already in the record, saving the record around each."""
    for task in story[len(record["tasks"]):]:
        started = time.monotonic()
        before_task = heads(acc, repo)  # before the switch, so a branch this task creates is rewound too
        if acc.git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{task.branch}", ok=False):
            acc.git(repo, "switch", "-q", task.branch)
        else:
            acc.git(repo, "switch", "-q", "-c", task.branch)
        sid = str(uuid.uuid4())
        entry = {"id": task.id, "branch": task.branch, "heads": before_task, "session": sid,
                 "outcome": "running", "reason": None, "reply": None, "tools": [], "commit": None}
        record["tasks"].append(entry)
        save(acc.tmp, record)
        try:
            before = acc.git(repo, "rev-parse", "HEAD")
            prompt = (f"{task.prompt}\n\n{UNATTENDED}\n\nWhen the work is done, run `git add -A`, and then "
                      f"`{acc.commit} \"{task.message}\"`, as two separate commands. "
                      "Reply with one sentence.")
            reason = None
            try:
                done = acc.claude(repo, prompt, session=sid, tools=STORY_TOOLS)
                entry.update(session=done.session_id, reply=done.text[:REPLY_CHARS], tools=done.tools)
                if done.is_error:
                    reason = "claude reported an error"
            except Fail as e:  # a timeout, or no result
                reason = f"claude: {e}"
            sid = entry["session"]
            subject = acc.git(repo, "log", "-1", "--format=%s")
            moved = acc.git(repo, "rev-parse", "HEAD") != before
            if moved and subject != task.message:
                reason = (reason + "; " if reason else "") + f"committed as {subject!r}, not {task.message!r}"
            elif not moved:
                reason = (reason + "; " if reason else "") + f"no commit {task.message!r}"
            if reason is None:
                acc.wait_ended(repo, sid)
                acc.checkpoint(repo, acc.git(repo, "rev-parse", "HEAD"))
            else:
                reason += end_session(acc, repo, sid)
                if not moved:  # the recorder commits the work, so the next tasks build on it
                    acc.git(repo, "add", "-A")
                    acc.git(repo, "commit", "-q", "--allow-empty", "-m", task.message)
            store = acc.store(repo)
            found = [f"{p} ({name})" for name in ("raw", "records") for p in scan_files(store / name, banned)]
            check(not found, f"task {task.id}: the store holds private data, stopping: " + "; ".join(found))
            entry["commit"] = acc.git(repo, "rev-parse", "HEAD")
            if task.merge:
                acc.git(repo, "switch", "-q", "main")
                acc.git(repo, "merge", "-q", "--no-ff", "-m", f"Merge {task.branch}", task.branch)
            entry.update(outcome="ok" if reason is None else "failed", reason=reason)
            save(acc.tmp, record)
        except BaseException as e:  # a stop: the entry says why, and resume starts here
            entry.update(outcome="failed", reason=str(e) or type(e).__name__)
            save(acc.tmp, record)
            raise
        print(f"{len(record['tasks']):2}/{len(story)} {task.id:22} {task.kind:9} session {sid[:8]} "
              f"({time.monotonic() - started:.0f}s)" + (f"  FAILED: {reason}" if reason else ""), flush=True)
    acc.git(repo, "switch", "-q", "main")


# --- resume: rewinding a recording ---------------------------------------------------------------

def resume_point(record: dict, story: list[Task], start: str | None) -> int:
    """The index of the task to replay from; the story before it must be the recorded one."""
    done = record["tasks"]
    ids = [e["id"] for e in done]
    if start is None:
        k = next((n for n, (_, e) in enumerate(zip(story, done)) if e["outcome"] != "ok"), min(len(done), len(story)))
    elif start in ids:
        k = ids.index(start)
    elif len(done) < len(story) and story[len(done)].id == start:
        k = len(done)
    else:
        raise Fail(f"--from {start}: not a recorded task, nor the next one to play "
                   f"({story[len(done)].id if len(done) < len(story) else 'none left'})")
    check(k < len(story) or k == len(done),
          f"--limit {len(story)} ends before task {k + 1}, {ids[k] if k < len(ids) else ''}")
    played = [t.id for t in story[:k]]
    check(played == ids[:k], f"the tasks file no longer starts with the {k} recorded tasks before the resume "
                             f"point: recorded {ids[:k]}, the file has {played}")
    return k


def store_files(store: Path) -> list[Path]:
    return sorted(p for name in ("raw", "records") for p in (store / name).rglob("*") if p.is_file())


def owner(rel: tuple[str, ...]) -> tuple[str, str] | None:
    """Whose a file under raw/ or records/ is: ("session", id) or ("commit", sha); None when unknown."""
    match rel:
        case ("records", "sessions", name) if name.endswith(".json"):
            return "session", name[:-5]
        case ("records", "checkpoints", name) if name.endswith(".json"):
            return "commit", name[:-5]
        case ("raw", "sessions", name) if (m := RAW_NAME.fullmatch(name)):
            return "session", m["sid"]
        case ("raw", "subagents", sid, _):
            return "session", sid
    return None


@dataclass
class Rewind:
    keep: set[str]  # the sessions of the tasks before the resume point
    target: dict[str, str]  # branch heads to restore
    deleted: list[str]  # branches created at or after the resume point
    files: list[Path]  # store files of the other sessions and of the rewound commits
    state: list[Path]  # host-local hook state of the other sessions, and of anything in flight
    sessions: list[str]  # the sessions removed


def plan_rewind(acc: Acc, repo: Path, record: dict, k: int) -> Rewind:
    entries = record["tasks"]
    keep = {e["session"] for e in entries[:k]}
    target = entries[k]["heads"] if k < len(entries) else heads(acc, repo)
    reachable = set(acc.git(repo, "rev-list", *target.values()).split())
    store = acc.store(repo)
    files, sessions = [], set()
    for path in store_files(store):
        who = owner(path.relative_to(store).parts)
        if path.name.startswith("."):  # an atomic write's leftover
            files.append(path)
        elif who and who[0] == "session" and who[1] not in keep:
            files.append(path)
            sessions.add(who[1])
        elif who and who[0] == "commit" and who[1] not in reachable:
            files.append(path)
    state = store / "state"
    turns = [p for p in sorted((state / "turns").glob("*")) if p.name.split(".json")[0] not in keep]
    locks = [p for p in sorted((state / "locks").glob("worker-*.lock*"))
             if p.name.removeprefix("worker-").split(".lock")[0] not in keep]
    sessions |= {p.name.split(".json")[0] for p in turns}
    in_flight = [p for p in (state / "active.json", state / "materialized.json", state / "rewrites") if p.exists()]
    return Rewind(keep, target, sorted(set(heads(acc, repo)) - set(target)), files,
                  turns + locks + in_flight, sorted(sessions))


def rebuild_history(acc: Acc, repo: Path, store: Path) -> None:
    """Point refs/wscontext/history at one root commit holding exactly the local raw/ and records/.

    The throwaway repository has no remote, so no shared history is rewritten, and the removed
    sessions' blobs are no longer reachable from the ref."""
    files = store_files(store)
    if not files:
        acc.git(repo, "update-ref", "-d", HISTORY_REF, ok=False)
        return
    done = acc.run(["git", "hash-object", "-w", "--no-filters", "--stdin-paths"], repo,
                   stdin="".join(f"{p}\n" for p in files))
    check(done.returncode == 0, f"git hash-object failed: {done.stderr.strip()}")
    index = acc.tmp / ".history.index"
    index.unlink(missing_ok=True)
    env = {"GIT_INDEX_FILE": str(index)}
    listing = "".join(f"100644 {sha}\t{p.relative_to(store).as_posix()}\n" for p, sha in zip(files, done.stdout.split()))
    done = acc.run(["git", "update-index", "--add", "--index-info"], repo, stdin=listing, env=env)
    check(done.returncode == 0, f"git update-index failed: {done.stderr.strip()}")
    tree = acc.git(repo, "write-tree", env=env)
    index.unlink()
    acc.git(repo, "update-ref", HISTORY_REF, acc.git(repo, "commit-tree", tree, "-m", "wscontext: recorder rewind"))


def apply_rewind(acc: Acc, repo: Path, plan: Rewind) -> None:
    store = acc.store(repo)
    try:
        pruned = json.loads((store / "state" / "pruned.json").read_bytes())
    except (OSError, ValueError):
        pruned = None
    check(not pruned, f"the store pruned raw copies ({', '.join(sorted(pruned or []))[:300]}), past "
                      "retention.raw_days: the corpus would miss them; record again")
    acc.git(repo, "reset", "-q", "--hard")
    acc.git(repo, "clean", "-qfdx")
    acc.git(repo, "switch", "-q", "--detach")
    for branch in plan.deleted:
        acc.git(repo, "update-ref", "-d", f"refs/heads/{branch}")
    for branch, sha in plan.target.items():
        acc.git(repo, "update-ref", f"refs/heads/{branch}", sha)
    acc.git(repo, "checkout", "-q", "-f", "main")
    acc.git(repo, "clean", "-qfdx")
    for path in plan.files + plan.state:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    for folder in (store / "raw" / "subagents").glob("*"):
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    gone = set(plan.sessions)
    for log in sorted((store / "logs").glob("*.jsonl*")):
        lines = log.read_text(encoding="utf-8").splitlines(keepends=True)
        kept = [line for line in lines if _logged_session(line) not in gone]
        if len(kept) != len(lines):
            log.write_text("".join(kept), encoding="utf-8")
    rebuild_history(acc, repo, store)


def _logged_session(line: str) -> str | None:
    try:
        return json.loads(line).get("session_id")
    except (ValueError, AttributeError):
        return None


def print_rewind(acc: Acc, repo: Path, record: dict, k: int, plan: Rewind, story: list[Task]) -> None:
    entries = record["tasks"]
    current = heads(acc, repo)
    if k < len(entries):
        print(f"rewinds {len(entries) - k} recorded task(s):")
        for n, e in enumerate(entries[k:], k + 1):
            print(f"  {n:2}. {e['id']:22} {e['outcome']}" + (f": {e['reason']}" if e.get("reason") else ""))
    moved = [f"{b} to {sha[:8]}" for b, sha in plan.target.items() if current.get(b) != sha]
    print("branches: " + ("; ".join([*(f"reset {m}" for m in moved), *(f"delete {b}" for b in plan.deleted)])
                          or "unchanged"))
    commits = sum(1 for p in plan.files if p.parent.name == "checkpoints")
    print(f"store: removes {len(plan.files)} file(s) of {len(plan.sessions)} session(s) "
          f"{', '.join(s[:8] for s in plan.sessions) or ''} and {commits} rewound commit(s), and "
          f"{len(plan.state)} hook state file(s)")
    print(f"replays {len(story) - k} task(s):")
    for n, t in enumerate(story[k:], k + 1):
        print(f"  {n:2}. {t.id:22} {t.kind:9} {t.branch}{' (merged)' if t.merge else ''}: {t.message}")


# --- the corpus ----------------------------------------------------------------------------------

def stage_corpus(acc: Acc, repo: Path, record: dict, staged: Path) -> list[str]:
    """Copy into staged/corpus the store files of the accepted sessions and of the commits on the
    story's branches, and save those branches as staged/repo.bundle. Returns the files left out."""
    accepted = {e["session"] for e in record["tasks"] if e["outcome"] == "ok"}
    reachable = set(acc.git(repo, "rev-list", "--branches").split())
    store = acc.store(repo)
    left_out = []
    for path in store_files(store):
        rel = path.relative_to(store)
        who = owner(rel.parts)
        if who and who[0] == "commit" and who[1] in reachable:
            linked = set(json.loads(path.read_bytes()).get("session_ids") or [])
            check(linked <= accepted, f"commit {who[1][:12]} links session(s) {sorted(linked - accepted)} "
                                      "that no accepted task ran")
        elif not (who and who[0] == "session" and who[1] in accepted):
            left_out.append(rel.as_posix())
            continue
        (staged / "corpus" / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, staged / "corpus" / rel)
    names = [RAW_NAME.fullmatch(p.name) for p in (staged / "corpus" / "raw" / "sessions").glob("*.jsonl")]
    copies = {m["sid"] for m in names if m and not m["pc"]}
    missing = sorted(s for s in accepted
                     if s not in copies or not (staged / "corpus" / "records" / "sessions" / f"{s}.json").exists())
    check(not missing, f"accepted session(s) without a record or a transcript copy: {', '.join(missing)}")
    acc.git(repo, "bundle", "create", "-q", str(staged / "repo.bundle"), "HEAD", "--branches")
    return left_out


# --- re-trimming: the recorded corpus under the current reduction --------------------------------

def parsed_view(path: Path, sidechain: bool = False) -> tuple:
    """What the adapter reads from a copy: its format, the turns with their items, the subagent ids."""
    p = ClaudeCodeAdapter().parse(path, sidechain=sidechain)
    return p.supported, p.format_version, [asdict(t) for t in p.turns], [r.agent_id for r in p.subagents]


def retrim(out: Path, source_dir: Path) -> str:
    """Store the corpus's transcript copies again from their source transcripts, as a capture with
    the current reduction and redactor would, and update each record's stored sizes. File names,
    the records' other fields, the checkpoints and repo.bundle stay. Writes nothing, and fails, when
    a source no longer has the size its record holds, a copy parses differently afterwards, or the
    result holds private data. Returns a summary line."""
    corpus, bundle = out / "corpus", out / "repo.bundle"
    check(bundle.is_file() and (corpus / "records").is_dir(), f"no recorded corpus and bundle in {out}")
    with tempfile.TemporaryDirectory(prefix="wscontext-retrim-") as tmp:
        clone = Path(tmp) / "clone"
        subprocess.run(["git", "clone", "-q", str(bundle), str(clone)], check=True, capture_output=True,
                       env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull})
        check((clone / ".wscontext" / "config.toml").is_file(), f"{bundle}: no .wscontext/config.toml at HEAD")
        config = load_config(clone)  # the settings the story repository was captured under
        staged = Path(tmp) / "corpus"
        shutil.copytree(corpus, staged)
        copies = sorted((staged / "raw" / "sessions").glob("*.jsonl"))
        sids = []
        before = after = source_total = subagent_copies = 0
        for raw in copies:
            m = RAW_NAME.fullmatch(raw.name)
            check(m is not None and not m["pc"], f"{raw.name}: not a session copy; a snapshot has no source to re-trim")
            sid = m["sid"]
            sids.append(sid)
            source = source_dir / f"{sid}.jsonl"
            path = staged / "records" / "sessions" / f"{sid}.json"
            check(source.is_file(), f"session {sid}: no source transcript {source}")
            check(path.is_file(), f"session {sid}: no record")
            rec = read_record(path, SessionRecord)
            refs = [r for r in ClaudeCodeAdapter().parse(source).subagents if r.path and is_safe_name(r.agent_id)]
            source_bytes = source.stat().st_size + sum(r.path.stat().st_size for r in refs)
            recorded = (rec.transcript_bytes or {}).get("source")
            check(recorded == source_bytes, f"session {sid}: the source transcripts hold {source_bytes} bytes and "
                                            f"the record {recorded}; they changed after the capture")
            subagents = staged / "raw" / "subagents" / sid
            stored = sorted(p.stem for p in subagents.glob("*.jsonl"))
            check(stored == sorted(r.agent_id for r in refs),
                  f"session {sid}: subagent copies {stored}, the source names {sorted(r.agent_id for r in refs)}")
            views = [parsed_view(raw)] + [parsed_view(subagents / f"{a}.jsonl", True) for a in stored]
            copy_reduced(source, raw, config)
            for r in refs:
                copy_reduced(r.path, subagents / f"{r.agent_id}.jsonl", config)
            check(views == [parsed_view(raw)] + [parsed_view(subagents / f"{a}.jsonl", True) for a in stored],
                  f"session {sid}: the re-trimmed copies parse differently")
            before += rec.transcript_bytes.get("stored", 0)
            rec.transcript_bytes = {**stored_sizes(raw, subagents), "source": source_bytes}
            after += rec.transcript_bytes["stored"]
            source_total += source_bytes
            subagent_copies += len(refs)
            write_record(path, rec, config)
        records = sorted(p.stem for p in (staged / "records" / "sessions").glob("*.json"))
        check(records == sorted(sids), f"records without a transcript copy: {sorted(set(records) - set(sids))}")
        found = scan(staged, bundle, forbidden())
        check(not found, "nothing written; the re-trimmed corpus holds private data: " + "; ".join(found))
        shutil.rmtree(corpus)
        shutil.copytree(staged, corpus)
    return (f"re-trimmed {len(sids)} sessions and {subagent_copies} subagent copies in {corpus}: stored "
            f"{before:,} -> {after:,} bytes, {after / source_total:.1%} of {source_total:,} source bytes")


def resume_command(args, work: Path) -> str:
    cmd = [".venv/bin/python", "acceptance/record_corpus.py", "--resume", str(work)]
    for flag, value, default in (("--tasks", args.tasks, None), ("--limit", args.limit, None),
                                 ("--out", args.out, OUT), ("--model", args.model, None),
                                 ("--max-budget-usd", args.max_budget_usd, 1.0),
                                 ("--plugin-root", args.plugin_root, None), ("--plugin-data", args.plugin_data, None)):
        if value is not None and value != default:
            cmd += [flag, str(value)]
    return shlex.join(cmd + [f for f, on in (("--force", args.force), ("--keep", args.keep)) if on])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="validate the story and print the plan, or with --resume what would be rewound; no API call")
    parser.add_argument("--check", action="store_true", help="scan the recorded corpus and bundle; no API call")
    parser.add_argument("--force", action="store_true", help="replace an already recorded corpus")
    parser.add_argument("--limit", type=int, help="play only the first N tasks of the story (a trial run)")
    parser.add_argument("--keep", action="store_true", help="keep the working directory after a successful run")
    parser.add_argument("--resume", type=Path, metavar="DIR", help="continue the recording kept in DIR")
    parser.add_argument("--from", dest="start", metavar="TASK_ID",
                        help="with --resume: replay from this task (default: the first failed or unplayed one)")
    parser.add_argument("--retrim", type=Path, metavar="SOURCE_DIR",
                        help="store the recorded corpus's copies again from the source transcripts in SOURCE_DIR")
    parser.add_argument("--tasks", type=Path,
                        help="the story (default: eval/stage2/tasks.toml, or on --resume the recorded one)")
    parser.add_argument("--out", type=Path, default=OUT, help="where corpus/ and repo.bundle go (default: eval/stage2)")
    parser.add_argument("--model", help="model for headless sessions (default: Claude Code's default)")
    parser.add_argument("--max-budget-usd", type=float, default=1.0, help="API spend cap per claude -p call")
    parser.add_argument("--plugin-root", help="plugin root to run ctx from (default: detected)")
    parser.add_argument("--plugin-data", help="plugin data directory (default: ~/.claude/plugins/data/wscontext-wscontext, "
                                              "or on --resume the recorded one)")
    args = parser.parse_args(argv)
    if args.start and not args.resume:
        parser.error("--from needs --resume")
    if args.retrim and (args.resume or args.check or args.dry_run):
        parser.error("--retrim goes alone")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
    work = None
    try:
        if args.check:
            check_recorded(args.out)
            return 0
        if args.retrim:
            print(retrim(args.out, args.retrim.expanduser()))
            return 0
        record = None
        if args.resume:
            work = args.resume.resolve()
            record = load_record(work)
        tasks_file = args.tasks or (Path(record["tasks_file"]) if record else OUT / "tasks.toml")
        tasks = load_tasks(tasks_file)
        if args.limit is not None:
            check(args.limit >= 1, "--limit wants at least 1")
            tasks = tasks[:args.limit]
        story = Acc(Path("<tmp>"), None, args.model, args.max_budget_usd, env, isolation(PLUGIN_ID))
        k = resume_point(record, tasks, args.start) if record else 0
        if args.dry_run:
            if record:
                acc = Acc(work, None, args.model, args.max_budget_usd, env)
                print(f"resume {work} from task {k + 1}" + (f", {tasks[k].id}" if k < len(tasks) else ""))
                print_rewind(acc, work / REPO, record, k, plan_rewind(acc, work / REPO, record, k), tasks)
            else:
                for n, t in enumerate(tasks, 1):
                    print(f"{n:2}. {t.id:22} {t.kind:9} {t.branch}{' (merged)' if t.merge else ''}: {t.message}")
                cmd = story.claude_cmd("<session-id>", False, STORY_TOOLS)
                print("\nclaude command, run in the throwaway repository, prompt on stdin:\n  " + shlex.join(cmd))
            print(f"\n{len(tasks) - k} sessions; nothing run")
            return 0
        corpus, bundle = args.out / "corpus", args.out / "repo.bundle"
        if (corpus.exists() or bundle.exists()) and not args.force:
            sys.exit(f"{corpus} or {bundle} already exists; the corpus is recorded once, pass --force to replace it")
        for tool in ("git", "uv", "claude"):
            if shutil.which(tool) is None:
                sys.exit(f"{tool} is not on PATH")
        banned = forbidden()
        plugin = locate_plugin(args.plugin_root, args.plugin_data or (record["plugin_data"] if record else None))
        print(f"plugin root: {plugin.root}\nplugin version: {plugin.version}\nplugin data: {plugin.data}")
        if record:
            acc = Acc(work, plugin, args.model, args.max_budget_usd, env, story.extra)
            repo = work / REPO
            print(f"resuming {work} from task {k + 1}\n")
            plan = plan_rewind(acc, repo, record, k)
            print_rewind(acc, repo, record, k, plan, tasks)
            apply_rewind(acc, repo, plan)
            del record["tasks"][k:]
            record["tasks_file"] = str(tasks_file.resolve())
            save(work, record)
            acc.ctx(repo, "init")  # restores what the clean removed before the first commit, and the hooks
            acc.ctx(repo, "rebuild")
            print()
        else:
            work = Path(tempfile.mkdtemp(prefix="wscontext-corpus-")).resolve()
            print(f"working directory: {work}\n")
            acc = Acc(work, plugin, args.model, args.max_budget_usd, env, story.extra)
            record = {"version": 1, "tasks_file": str(tasks_file.resolve()), "plugin_data": str(plugin.data),
                      "started": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tasks": []}
            save(work, record)
            repo = setup_repo(acc)
        Path(acc.commit).write_text(COMMIT_SCRIPT)
        Path(acc.commit).chmod(0o755)
        try:
            play(acc, repo, tasks, banned, record)
        except (Fail, KeyboardInterrupt) as e:
            print(f"\nstopped: {e or 'interrupted'}", file=sys.stderr)
            report_failures(record)
            print(f"\nnothing written; kept {work}\nresume with:\n  {resume_command(args, work)}")
            return 130 if isinstance(e, KeyboardInterrupt) else 1
        if failures(record):
            report_failures(record)
            print(f"\nnothing written; kept {work}\nfix the story, then resume from task "
                  f"{failures(record)[0][0]}:\n  {resume_command(args, work)}")
            return 1
        time.sleep(2)  # let the last detached workers finish writing
        staged = work / "staged"
        shutil.rmtree(staged, ignore_errors=True)
        left_out = stage_corpus(acc, repo, record, staged)
        if left_out:
            print(f"left out {len(left_out)} store file(s) of no accepted session or story commit: "
                  + ", ".join(left_out[:10]) + (" ..." if len(left_out) > 10 else ""))
        found = scan(staged / "corpus", staged / "repo.bundle", banned)
        check(not found, "nothing written; the recording holds private data: " + "; ".join(found))
        for target in (corpus, bundle):
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
        args.out.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staged / "corpus", corpus)
        shutil.copy2(staged / "repo.bundle", bundle)
        size = sum(p.stat().st_size for p in args.out.rglob("*") if p.is_file() and p.name != "tasks.toml")
        print(f"\nrecorded {len(record['tasks'])} sessions and "
              f"{len(acc.git(repo, 'rev-list', '--branches').splitlines())} commits into {args.out} "
              f"({size / 1024 / 1024:.1f} MB)")
        if args.keep:
            print(f"kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    except Fail as e:
        print(f"FAIL: {e}", file=sys.stderr)
        if work and (work / STATE).exists():
            print(f"kept {work}" + (f"\nresume with:\n  {resume_command(args, work)}"
                                    if (work / REPO / ".git").is_dir() else ""), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
