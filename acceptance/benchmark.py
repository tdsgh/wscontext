"""The on/off benchmark of docs/wscontext-phase-3.md §11: each task headless, with wscontext retrieval off and on.

    .venv/bin/python acceptance/benchmark.py [--runs N] [--tasks a,b] [--max-budget-usd X] [--keep]
    .venv/bin/python acceptance/benchmark.py --dry-run [--runs N] [--tasks a,b]
    .venv/bin/python acceptance/benchmark.py --verify-checks [--tasks a,b]
    .venv/bin/python acceptance/benchmark.py --list

Not part of the offline suite (pytest never collects `acceptance/`). Plays eval/bench/tasks.toml:
per run, per task, first with `[retrieval] enabled = false` and then with `true`, one `claude -p`
session in a fresh throwaway clone of the stage 2 story repository (eval/stage2/repo.bundle at
main) whose store holds the recorded corpus (eval/stage2/corpus). Before the session it runs
`ctx init`, commits the arm's switch and runs `ctx rebuild`, so both arms start from a built index
and graph. After the session ends it copies the task's check in and runs it with the repository's
own tests; the task is done when both pass. Figures come from `ctx stats session` in that clone and
from claude's own result line (turns, cost). Writes eval/bench/runs/<date>-<plugin version>.json
and .md.

Spends API usage: one session per task, per arm, per run (10 tasks x 2 arms = 20 sessions by
default), each capped by --max-budget-usd. `--dry-run`, `--list` and `--verify-checks` make no
claude call: `--dry-run` validates the tasks and prints the plan; `--verify-checks` runs each check
against the untouched story (it must fail) and with eval/bench/solutions/<task>.patch applied (it
must pass), offline, without the plugin.

The sessions use the corpus recorder's isolation: no user settings, CLAUDE.md, rules, auto memory
or MCP servers; only the wscontext plugin is enabled. `ctx` runs through the installed plugin's
launcher, as in the acceptance runs, and only ever inside the temporary directory.
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from record_corpus import ROOT, STORY_TOOLS, end_session, isolation
from stage1 import PLUGIN_ID, Acc, Fail, check, locate_plugin, wait_for
from stage2 import set_config

BENCH = ROOT / "eval" / "bench"
STORY = ROOT / "eval" / "stage2"
ARMS = ("off", "on")  # the order each task runs in
KINDS = {"where", "impact", "history"}
FIELDS = {"id", "kind", "prompt", "check"}
# the recorder's tools, plus the search tools and the Git history commands an agent reaches for
BENCH_TOOLS = STORY_TOOLS + " Grep Glob Bash(git blame:*) Bash(git branch:*)"
UNATTENDED = ("This is an unattended run: nobody will answer questions or grant permissions. Run every "
              "command from the repository root, without cd or environment variable prefixes; plain rm, "
              "mkdir and cd are refused, and the Write tool creates directories. Do not commit. When you "
              "are done, reply with one sentence.")
CHECK_FILE = "bench_check.py"  # where a task's check is copied, at the repository root
CHECK_TIMEOUT = 300
OUTPUT_TAIL = 1500
DEFAULT_BUDGET = 2.0
# report columns: (row key, heading); every row puts "done" before them
FIGURES = (("turns", "turns"), ("tool_calls", "tool calls"), ("search", "search"), ("read", "read"),
           ("edit", "edit"), ("shell", "shell"), ("ctx", "ctx"), ("tokens", "tokens"),
           ("output_tokens", "output tokens"), ("seconds", "seconds"), ("cost_usd", "cost $"))
GROUPS = ("search", "read", "edit", "shell")  # tool-call kinds with their own column; the rest is "other"


@dataclass
class Task:
    id: str
    kind: str
    prompt: str
    check: str  # relative to the tasks file's folder


def load_tasks(path: Path) -> list[Task]:
    tasks = []
    for raw in tomllib.loads(path.read_text(encoding="utf-8")).get("task", []):
        unknown, missing = set(raw) - FIELDS, FIELDS - set(raw)
        check(not unknown, f"{path.name}: task {raw.get('id')!r} has unknown fields {sorted(unknown)}")
        check(not missing, f"{path.name}: task {raw.get('id')!r} lacks {sorted(missing)}")
        tasks.append(Task(**{k: str(v) for k, v in raw.items()}))
    check(tasks, f"{path.name} holds no tasks")
    ids = [t.id for t in tasks]
    check(len(set(ids)) == len(ids), f"{path.name}: duplicate task ids")
    for t in tasks:
        check(t.id and "," not in t.id and "/" not in t.id, f"{path.name}: task id {t.id!r}: no commas or slashes")
        check(t.kind in KINDS, f"{path.name}: task {t.id!r} has kind {t.kind!r}, wanted one of {sorted(KINDS)}")
        check(t.prompt.strip(), f"{path.name}: task {t.id!r} has an empty prompt")
        source = (path.parent / t.check).resolve()
        check(source.is_relative_to(path.parent.resolve()) and source.is_file(),
              f"{path.name}: task {t.id!r}: no check file {t.check}")
        text = source.read_text(encoding="utf-8")
        try:
            compile(text, str(source), "exec")
        except SyntaxError as e:
            raise Fail(f"{path.name}: task {t.id!r}: {t.check} does not compile: {e}")
        check("unittest.main()" in text, f"{path.name}: task {t.id!r}: {t.check} is not a runnable unittest file")
    return tasks


def select(tasks: list[Task], names: str | None) -> list[Task]:
    if not names:
        return tasks
    wanted = [n.strip() for n in names.split(",") if n.strip()]
    unknown = sorted(set(wanted) - {t.id for t in tasks})
    check(not unknown, f"unknown task(s): {', '.join(unknown)}; --list shows them")
    return [t for t in tasks if t.id in wanted]


def prompt(task: Task) -> str:
    return f"{task.prompt}\n\n{UNATTENDED}"


# --- one session ---------------------------------------------------------------------------------

def clone_story(acc: Acc, repo: Path) -> None:
    """A clone of the story bundle at main, with its branches as local branches and no remote."""
    acc.git(acc.tmp, "clone", "-q", str(STORY / "repo.bundle"), str(repo))
    for ref in acc.git(repo, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/").split():
        branch = ref.removeprefix("origin/")
        if branch not in ("HEAD", "origin", "main"):
            acc.git(repo, "branch", "-q", branch, ref)
    acc.git(repo, "remote", "remove", "origin")
    acc.identify(repo)


def prepare(acc: Acc, repo: Path, arm: str) -> str:
    """Clone, corpus into the store, the arm's switch committed, index and graph built; returns the story's head."""
    clone_story(acc, repo)
    head = acc.git(repo, "rev-parse", "HEAD")
    store = acc.store(repo)
    for name in ("raw", "records"):
        shutil.copytree(STORY / "corpus" / name, store / name, dirs_exist_ok=True)
    set_config(repo, "retrieval", "enabled", "true" if arm == "on" else "false")
    acc.ctx(repo, "init")  # the CLAUDE.md block follows the switch
    acc.git(repo, "add", "-A")
    acc.git(repo, "commit", "-q", "--allow-empty", "-m", f"Benchmark arm: retrieval {arm}")
    acc.ctx(repo, "rebuild")  # the session pays for answers, not for the first build
    return head


def run_check(acc: Acc, repo: Path, source: Path) -> dict:
    """Copy the check in and run it, then the repository's own tests; done when both pass."""
    shutil.copyfile(source, repo / CHECK_FILE)
    # bytecode compiled before an edit of the same size in the same second would still be used
    fresh = {"PYTHONPYCACHEPREFIX": tempfile.mkdtemp(prefix="pycache-", dir=acc.tmp)}
    out: dict = {}
    for name, cmd in (("check", [sys.executable, CHECK_FILE]),
                      ("suite", [sys.executable, "-m", "unittest", "discover", "-s", "tests"])):
        try:
            done = acc.run(cmd, repo, timeout=CHECK_TIMEOUT, env=fresh)
            out[name], text = done.returncode == 0, done.stdout + done.stderr
        except subprocess.TimeoutExpired:
            out[name], text = False, f"timed out after {CHECK_TIMEOUT}s"
        if not out[name]:
            out[f"{name}_output"] = text[-OUTPUT_TAIL:]
    out["done"] = out["check"] and out["suite"]
    return out


def claude_result(run: str | None) -> dict:
    """The model, agent turns, cost and duration from claude's stream-json output."""
    found: dict = {"model": None, "turns": None, "cost_usd": None, "wall_seconds": None, "result": None}
    if run is None:
        return found
    try:
        lines = Path(f"{run}.out").read_text().splitlines()
    except OSError:
        return found
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            found["model"] = event.get("model")
        elif event.get("type") == "result":
            ms = event.get("duration_ms")
            found.update(turns=event.get("num_turns"), cost_usd=event.get("total_cost_usd"),
                         wall_seconds=ms / 1000 if isinstance(ms, (int, float)) else None,
                         result=event.get("subtype"))
    return found


def figures(results: list[dict]) -> dict:
    """The per-session figures from the blocks of `ctx --json --full stats session <id>`."""
    merged: dict = {}
    for block in results:
        merged.update(block)
    kinds = dict.fromkeys(GROUPS + ("other",), 0)
    for kind, n in (merged.get("tool_calls") or {}).items():  # counted by kind, a Bash call that ran ctx as "ctx"
        kinds[kind if kind in GROUPS else "other"] += n
    tokens = merged.get("tokens") or {}
    times = [t["seconds"] for t in merged.get("turns") or [] if t.get("seconds") is not None]
    return {"prompts": len(merged.get("turns") or []), "tool_calls": sum(kinds.values()), **kinds,
            "tool_kinds": merged.get("tool_calls") or {},
            "ctx": sum((merged.get("ctx_calls") or {}).values()), "ctx_calls": merged.get("ctx_calls") or {},
            "tokens": sum(tokens.values()) if tokens else None, "output_tokens": tokens.get("output"),
            "token_kinds": tokens or None, "seconds": sum(times) if times else None,
            "ended_by": merged.get("ended_by"), "retrieval": merged.get("retrieval")}


def run_session(acc: Acc, task: Task, arm: str, n: int) -> dict:
    repo = acc.tmp / f"{task.id}-{arm}-{n}"
    head = prepare(acc, repo, arm)
    sid = str(uuid.uuid4())
    row: dict = {"task": task.id, "kind": task.kind, "arm": arm, "run": n, "session": sid,
                 "story_head": head, "error": None}
    run, failed = None, False
    try:
        sid, proc, run = acc.claude_start(repo, prompt(task), session=sid, tools=BENCH_TOOLS)
        done = acc.claude_finish(sid, proc, run)
        row["session"] = sid = done.session_id
        failed = done.is_error
    except Fail as e:  # a timeout, or no result
        row["error"] = f"claude: {e}"
    row.update(claude_result(run))
    if failed:  # the spend cap ends a session this way too
        row["error"] = f"claude reported an error ({row['result']})"
    row["error"] = ((row["error"] or "") + end_session(acc, repo, sid)).removeprefix("; ") or None
    row.update(run_check(acc, repo, BENCH / task.check))
    try:
        results = wait_for(lambda: (r := acc.ctx_json(repo, "--full", "stats", "session", sid)["results"])
                           and r[0].get("ended_by") and r, f"session {sid} to end in the store")
        row.update(figures(results))
    except Fail as e:
        row["error"] = "; ".join(x for x in (row["error"], f"no session figures: {e}") if x)
    return row


# --- the report ----------------------------------------------------------------------------------

def mean(values: list) -> float | None:
    found = [v for v in values if v is not None]
    return sum(found) / len(found) if found else None


def aggregate(rows: list[dict]) -> dict:
    """One task and arm over its runs: runs, runs done, and the mean of each figure."""
    return {"runs": len(rows), "done": sum(bool(r.get("done")) for r in rows),
            **{key: mean([r.get(key) for r in rows]) for key, _ in FIGURES}}


def total(per_task: list[dict]) -> dict:
    """Over tasks: runs done of runs, and each figure summed over the tasks' means."""
    return {"runs": sum(a["runs"] for a in per_task), "done": sum(a["done"] for a in per_task),
            **{key: (sum(v for a in per_task if (v := a[key]) is not None)
                     if any(a[key] is not None for a in per_task) else None) for key, _ in FIGURES}}


def summarize(rows: list[dict]) -> dict:
    tasks = list(dict.fromkeys(r["task"] for r in rows))
    by = {t: {arm: aggregate([r for r in rows if r["task"] == t and r["arm"] == arm]) for arm in ARMS}
          for t in tasks}
    both = [t for t in tasks if all(by[t][arm]["runs"] and by[t][arm]["done"] == by[t][arm]["runs"] for arm in ARMS)]
    return {"tasks": by,
            "total": {arm: total([by[t][arm] for t in tasks if by[t][arm]["runs"]]) for arm in ARMS},
            "done_in_both": {"tasks": both, **{arm: total([by[t][arm] for t in both]) for arm in ARMS}}}


def cell(key: str, value) -> str:
    if value is None:
        return "-"
    if key == "cost_usd":
        return f"{value:.2f}"
    if key == "seconds":
        return f"{value:.0f}"
    return f"{value:,.0f}" if float(value).is_integer() or value >= 100 else f"{value:.1f}"


def markdown(doc: dict) -> str:
    meta, summary = doc["meta"], doc["summary"]
    heads = " | ".join(h for _, h in FIGURES)
    line = "|" + " --- |" * (len(FIGURES) + 2)

    def row(label: str, a: dict) -> str:
        return f"| {label} | {a['done']}/{a['runs']} | " + " | ".join(cell(k, a[k]) for k, _ in FIGURES) + " |"

    out = [f"# wscontext on/off benchmark, {meta['date'][:10]}, plugin {meta['plugin_version']}", "",
           f"Model {meta['model'] or 'Claude Code default'}; {meta['runs']} run(s) per task and arm; "
           f"{len(meta['tasks'])} tasks; spend cap ${meta['max_budget_usd']:.2f} per session; "
           f"story {meta['story']} at {(meta['story_head'] or '?')[:7]}"
           + ("; **interrupted, partial**" if not meta["complete"] else "") + ".", "",
           "Done means the task's check and the repository's own tests passed after the session. "
           "Every figure is a mean over the runs of a task; totals add the tasks up.", "",
           "## Totals", "", f"| arm | done | {heads} |", line]
    out += [row(arm, summary["total"][arm]) for arm in ARMS]
    both = summary["done_in_both"]
    out += ["", f"Tasks done in every run of both arms ({len(both['tasks'])}: "
                f"{', '.join(both['tasks']) or 'none'}):", "", f"| arm | done | {heads} |", line]
    out += [row(arm, both[arm]) for arm in ARMS]
    out += ["", "## Per task", "", f"| task | arm | done | {heads} |", "|" + " --- |" * (len(FIGURES) + 3)]
    for task, arms in summary["tasks"].items():
        out += [row(f"{task} | {arm}", arms[arm]) for arm in ARMS if arms[arm]["runs"]]
    missed = [r for r in doc["runs"] if not r.get("done") or r.get("error")]
    if missed:
        out += ["", "## Sessions not done or with an error", ""]
        for r in missed:
            why = [x for x in (r.get("error"), None if r.get("check") else "check failed",
                               None if r.get("suite") else "repository tests failed") if x]
            out.append(f"- {r['task']}, {r['arm']}, run {r['run']} (session {r['session']}): {'; '.join(why)}")
    out += ["", "Turns are the agent's turns as claude reports them. Search is Grep, Glob, WebSearch and "
                "ToolSearch; read is Read, LS and WebFetch; shell is a Bash call that ran no ctx; tool calls "
                "include the Bash calls that ran ctx; ctx counts ctx invocations. Tokens add input, output, cache read and cache "
                "creation. Seconds are the session's turn time from its record.", ""]
    return "\n".join(out)


def report(rows: list[dict], meta: dict) -> dict:
    return {"version": 1, "meta": meta, "summary": summarize(rows), "runs": rows}


def write_report(doc: dict, out: Path) -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{doc['meta']['date'][:10]}-{doc['meta']['plugin_version']}"
    name, n = stem, 1
    while (out / f"{name}.json").exists() or (out / f"{name}.md").exists():
        n += 1
        name = f"{stem}-{n}"
    paths = out / f"{name}.json", out / f"{name}.md"
    paths[0].write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    paths[1].write_text(markdown(doc), encoding="utf-8")
    return paths


# --- offline modes -------------------------------------------------------------------------------

def verify_checks(acc: Acc, tasks: list[Task]) -> list[str]:
    """Each check fails on the untouched story and passes with its solution; returns the problems."""
    problems = []
    for t in tasks:
        patch = BENCH / "solutions" / f"{t.id}.patch"
        repo = acc.tmp / t.id
        clone_story(acc, repo)
        before = run_check(acc, repo, BENCH / t.check)
        if patch.is_file():
            acc.git(repo, "apply", str(patch))
            after = run_check(acc, repo, BENCH / t.check)
        else:
            after = {"done": False, "check": False, "suite": False, "check_output": f"no {patch.name}"}
        found = [x for x in (None if before["suite"] else "the untouched story's tests fail",
                             "the check passes on the untouched story" if before["check"] else None,
                             None if after["check"] else "the check fails with the solution",
                             None if after["suite"] else "the story's tests fail with the solution") if x]
        problems += [f"{t.id}: {p}" for p in found]
        print(f"{'ok  ' if not found else 'FAIL'} {t.id:22} untouched: check {'passes' if before['check'] else 'fails'}"
              f"; with solution: check {'passes' if after['check'] else 'fails'}, tests "
              f"{'pass' if after['suite'] else 'fail'}" + (f"  ({'; '.join(found)})" if found else ""), flush=True)
        for key in ("check_output", "suite_output"):
            if found and after.get(key):
                print("    " + after[key].strip().replace("\n", "\n    "))
    return problems


def print_plan(tasks: list[Task], runs: int, budget: float, model: str | None, env: dict) -> None:
    sessions = len(tasks) * len(ARMS) * runs
    print(f"{len(tasks)} task(s) x {len(ARMS)} arms ({' then '.join(ARMS)}) x {runs} run(s) = "
          f"{sessions} claude -p session(s)")
    print(f"spend cap ${budget:.2f} per session, ${budget * sessions:.2f} at most in total "
          "(an upper bound, not an estimate)")
    corpus = len(list((STORY / "corpus" / "records" / "sessions").glob("*.json")))
    print(f"story: {(STORY / 'repo.bundle').relative_to(ROOT)} at main, with {corpus} recorded sessions "
          f"from {(STORY / 'corpus').relative_to(ROOT)}\n\ntasks:")
    for t in tasks:
        print(f"  {t.id:22} {t.kind:8} {t.check}")
    print("\nper task, arm and run, in a fresh clone under one temporary directory:\n"
          "  1. clone the bundle at main, its branches as local branches, no remote\n"
          "  2. copy the corpus raw/ and records/ into the clone's store\n"
          "  3. set [retrieval] enabled (off: false, on: true), ctx init, commit\n"
          "  4. ctx rebuild: search index and code graph\n"
          "  5. claude -p with the task's prompt; end the session\n"
          f"  6. copy the check in as {CHECK_FILE}; run it and the repository's tests\n"
          "  7. ctx --json --full stats session <id>\n"
          f"report: {(BENCH / 'runs').relative_to(ROOT)}/<date>-<plugin version>.json and .md")
    acc = Acc(Path("<tmp>"), None, model, budget, env, isolation(PLUGIN_ID))
    print("\nclaude command, run in the clone, prompt on stdin:\n  " + shlex.join(acc.claude_cmd("<session-id>", False, BENCH_TOOLS)))
    print(f"\nadded to every prompt:\n  {UNATTENDED}")


# --- main ----------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", type=int, default=1, help="runs per task and arm (default 1)")
    parser.add_argument("--tasks", metavar="A,B", help="run only these tasks")
    parser.add_argument("--dry-run", action="store_true", help="validate the tasks and print the plan; no claude call")
    parser.add_argument("--verify-checks", action="store_true",
                        help="run each check on the untouched story and with its solution; no claude call")
    parser.add_argument("--list", action="store_true", help="list the tasks and exit")
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory")
    parser.add_argument("--out", type=Path, default=BENCH / "runs", help="where the report goes (default: eval/bench/runs)")
    parser.add_argument("--model", help="model for the sessions (default: Claude Code's default)")
    parser.add_argument("--max-budget-usd", type=float, default=DEFAULT_BUDGET,
                        help=f"API spend cap per claude -p session (default {DEFAULT_BUDGET})")
    parser.add_argument("--plugin-root", help="plugin root to run ctx from (default: detected)")
    parser.add_argument("--plugin-data", help="plugin data directory (default: ~/.claude/plugins/data/wscontext-wscontext)")
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs wants at least 1")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
    try:
        tasks = select(load_tasks(BENCH / "tasks.toml"), args.tasks)
        check((STORY / "repo.bundle").is_file() and (STORY / "corpus" / "records").is_dir(),
              f"no story bundle and corpus in {STORY}")
    except Fail as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    if args.list:
        for t in tasks:
            print(f"{t.id:22} {t.kind:8} {t.prompt}")
        return 0
    if args.dry_run:
        print_plan(tasks, args.runs, args.max_budget_usd, args.model, env)
        print("\nnothing run")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="wscontext-bench-")).resolve()
    try:
        if args.verify_checks:
            problems = verify_checks(Acc(tmp, None, None, 0, env), tasks)
            print(f"\n{len(tasks) - len({p.split(':')[0] for p in problems})} of {len(tasks)} checks behave; "
                  f"problems: {'; '.join(problems) or 'none'}")
            return 1 if problems else 0
        for tool in ("git", "uv", "claude"):
            if shutil.which(tool) is None:
                sys.exit(f"{tool} is not on PATH")
        plugin = locate_plugin(args.plugin_root, args.plugin_data)
        print(f"plugin root: {plugin.root}\nplugin version: {plugin.version}\nplugin data: {plugin.data}")
        print(f"working directory: {tmp}\n")
        acc = Acc(tmp, plugin, args.model, args.max_budget_usd, env, extra=isolation(PLUGIN_ID))
        rows: list[dict] = []
        complete, code = False, 0
        total_sessions = len(tasks) * len(ARMS) * args.runs
        try:
            for n in range(1, args.runs + 1):
                for t in tasks:
                    for arm in ARMS:
                        started = time.monotonic()
                        r = run_session(acc, t, arm, n)
                        rows.append(r)
                        print(f"{len(rows):3}/{total_sessions} {t.id:22} {arm:3} run {n}: "
                              f"{'done    ' if r['done'] else 'not done'} turns {cell('turns', r.get('turns'))}, "
                              f"tools {cell('tool_calls', r.get('tool_calls'))}, ctx {cell('ctx', r.get('ctx'))}, "
                              f"${cell('cost_usd', r.get('cost_usd'))} ({time.monotonic() - started:.0f}s)"
                              + (f"  {r['error']}" if r.get("error") else ""), flush=True)
                        if not args.keep:
                            time.sleep(1)  # let detached workers finish writing
                            shutil.rmtree(tmp / f"{t.id}-{arm}-{n}", ignore_errors=True)
            complete = True
        except (Fail, KeyboardInterrupt) as e:
            print(f"\nstopped: {e or 'interrupted'}", file=sys.stderr)
            code = 130 if isinstance(e, KeyboardInterrupt) else 1
        acc.plugin.reconcile()
        if rows:
            meta = {"date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "plugin_version": acc.plugin.version, "plugin_root": str(acc.plugin.root),
                    "model": next((r["model"] for r in rows if r.get("model")), args.model),
                    "runs": args.runs, "tasks": [t.id for t in tasks], "arms": list(ARMS),
                    "max_budget_usd": args.max_budget_usd, "story": str((STORY / "repo.bundle").relative_to(ROOT)),
                    "story_head": rows[0]["story_head"], "complete": complete}
            doc = report(rows, meta)
            paths = write_report(doc, args.out)
            both = doc["summary"]["total"]
            print("\n" + "\n".join(f"{arm}: {both[arm]['done']}/{both[arm]['runs']} done, "
                                   f"${cell('cost_usd', both[arm]['cost_usd'])}" for arm in ARMS))
            print(f"report: {paths[0]}\n        {paths[1]}")
        return code
    except Fail as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    finally:
        if args.keep:
            print(f"\nkept {tmp}")
        else:
            time.sleep(1)
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
