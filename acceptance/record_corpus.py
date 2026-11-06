"""Record the stage-2 eval corpus: a scripted story played by headless sessions in a throwaway repository.

    .venv/bin/python acceptance/record_corpus.py [--dry-run | --check] [--force] [--keep]

Plays eval/stage2/tasks.toml, one `claude -p` session per task, in a throwaway repository under a
temporary directory with a fixed Git identity, each task committed. It then copies the store's
`raw/` and `records/` into eval/stage2/corpus/ and saves the repository as eval/stage2/repo.bundle,
so `ctx eval` can index the sessions and the commit messages. It fails, writing nothing, when the
corpus or the bundle holds the home directory path or the operator's email.

Spends API usage once (about 25 sessions); `--dry-run` validates the story and prints the plan
without a call, and `--check` scans an already recorded corpus and bundle without a call. The
recorded corpus is meant to be committed once and left alone: `--force` replaces it.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from stage1 import COMMIT_SCRIPT, TOOLS, Acc, Fail, check, locate_plugin, wait_for

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "eval" / "stage2"
IDENTITY = ("Corpus Recorder", "corpus@example.invalid")
KINDS = {"feature", "rejected", "bugfix", "refactor"}
# the stories run their own code to measure and test it
STORY_TOOLS = TOOLS + " Bash(python3:*)"


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
    """What must not appear in the recorded files, by name: the home directory and the operator's email."""
    found = {"home directory": str(Path.home()), "resolved home directory": str(Path.home().resolve())}
    done = subprocess.run(["git", "config", "--global", "user.email"], capture_output=True, text=True)
    if done.stdout.strip():
        found["operator email"] = done.stdout.strip()
    return found


def scan(root: Path, bundle: Path, banned: dict[str, str]) -> list[str]:
    """Where a banned string appears: in a corpus file, or in the bundle's history, patches included."""
    problems = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        data = path.read_bytes()
        problems += [f"{name} in {path.relative_to(root)}" for name, value in banned.items() if value.encode() in data]
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


def play(acc: Acc, tasks: list[Task]) -> Path:
    repo = acc.tmp / "tally"
    repo.mkdir()
    acc.git(repo, "init", "-q", "-b", "main")
    acc.git(repo, "config", "user.name", IDENTITY[0])
    acc.git(repo, "config", "user.email", IDENTITY[1])
    (repo / "README.md").write_text("# tally\n")
    acc.git(repo, "add", "README.md")
    acc.git(repo, "commit", "-q", "-m", "Start the tally project")
    acc.ctx(repo, "init")
    for n, task in enumerate(tasks, 1):
        started = time.monotonic()
        if acc.git(repo, "rev-parse", "--verify", "-q", task.branch, ok=False):
            acc.git(repo, "switch", "-q", task.branch)
        else:
            acc.git(repo, "switch", "-q", "-c", task.branch)
        prompt = (f"{task.prompt}\n\nWhen the work is done, run `git add -A`, and then "
                  f"`{acc.commit} \"{task.message}\"`, as two separate commands. "
                  "Reply with one sentence.")
        done = acc.claude(repo, prompt, tools=STORY_TOOLS)
        check(not done.is_error, f"task {task.id}: claude reported an error: {done.text[:300]}")
        check(acc.git(repo, "log", "-1", "--format=%s") == task.message,
              f"task {task.id}: no commit {task.message!r}; tools used: {done.tools}")
        acc.wait_ended(repo, done.session_id)
        acc.checkpoint(repo, acc.git(repo, "rev-parse", "HEAD"))
        if task.merge:
            acc.git(repo, "switch", "-q", "main")
            acc.git(repo, "merge", "-q", "--no-ff", "-m", f"Merge {task.branch}", task.branch)
        print(f"{n:2}/{len(tasks)} {task.id:22} {task.kind:9} session {done.session_id[:8]} "
              f"({time.monotonic() - started:.0f}s)", flush=True)
    acc.git(repo, "switch", "-q", "main")
    return repo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="validate the story and print the plan; no API call")
    parser.add_argument("--check", action="store_true", help="scan the recorded corpus and bundle; no API call")
    parser.add_argument("--force", action="store_true", help="replace an already recorded corpus")
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory")
    parser.add_argument("--tasks", type=Path, default=OUT / "tasks.toml", help="the story (default: eval/stage2/tasks.toml)")
    parser.add_argument("--out", type=Path, default=OUT, help="where corpus/ and repo.bundle go (default: eval/stage2)")
    parser.add_argument("--model", help="model for headless sessions (default: Claude Code's default)")
    parser.add_argument("--max-budget-usd", type=float, default=1.0, help="API spend cap per claude -p call")
    parser.add_argument("--plugin-root", help="plugin root to run ctx from (default: detected)")
    parser.add_argument("--plugin-data", help="plugin data directory (default: ~/.claude/plugins/data/wscontext-wscontext)")
    args = parser.parse_args(argv)
    try:
        if args.check:
            check_recorded(args.out)
            return 0
        tasks = load_tasks(args.tasks)
        if args.dry_run:
            for n, t in enumerate(tasks, 1):
                print(f"{n:2}. {t.id:22} {t.kind:9} {t.branch}{' (merged)' if t.merge else ''}: {t.message}")
            print(f"\n{len(tasks)} sessions; nothing run")
            return 0
        corpus, bundle = args.out / "corpus", args.out / "repo.bundle"
        if (corpus.exists() or bundle.exists()) and not args.force:
            sys.exit(f"{corpus} or {bundle} already exists; the corpus is recorded once, pass --force to replace it")
        for tool in ("git", "uv", "claude"):
            if shutil.which(tool) is None:
                sys.exit(f"{tool} is not on PATH")
        banned = forbidden()
        plugin = locate_plugin(args.plugin_root, args.plugin_data)
        print(f"plugin root: {plugin.root}\nplugin version: {plugin.version}")
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true"}
        tmp = Path(tempfile.mkdtemp(prefix="wscontext-corpus-")).resolve()
        print(f"working directory: {tmp}\n")
        acc = Acc(tmp, plugin, args.model, args.max_budget_usd, env)
        Path(acc.commit).write_text(COMMIT_SCRIPT)
        Path(acc.commit).chmod(0o755)
        try:
            repo = play(acc, tasks)
            store = acc.store(repo)
            sessions = wait_for(lambda: len(list((store / "records" / "sessions").glob("*.json"))) >= len(tasks)
                                and len(list((store / "records" / "sessions").glob("*.json"))),
                                f"{len(tasks)} session records")
            time.sleep(2)  # let the last detached workers finish writing
            staged = tmp / "staged"
            for name in ("raw", "records"):
                shutil.copytree(store / name, staged / "corpus" / name)
            acc.git(repo, "bundle", "create", "-q", str(staged / "repo.bundle"), "--all")
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
            print(f"\nrecorded {sessions} sessions and {len(acc.git(repo, 'rev-list', '--all').splitlines())} commits "
                  f"into {args.out} ({size / 1024 / 1024:.1f} MB)")
        finally:
            if args.keep:
                print(f"kept {tmp}")
            else:
                shutil.rmtree(tmp, ignore_errors=True)
    except Fail as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
