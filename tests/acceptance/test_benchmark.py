"""The on/off benchmark runner: its task file, setup, checks, figures and report, with no claude call."""

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from conftest import run_git

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli"))
from cli_support import spawned  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "acceptance"))
import benchmark as bm  # noqa: E402
from stage1 import Acc, Claude, Fail  # noqa: E402

from wscontext.cli.main import main as ctx_main
from wscontext.capture import worker
from wscontext.config import Config, load_config
from wscontext.history.branch import commit_paths
from wscontext.store.paths import StorePaths
from wscontext.store.records import SessionRecord, TurnRecord, write_session

ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_EDITOR": "true"}
STORY = bm.load_subject(bm.BENCH)
REAL_TASKS = bm.load_tasks(STORY.dir / "tasks.toml")
CHECK = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        pass\n\n\nunittest.main()\n"


@dataclass
class FakeAcc(Acc):
    """`ctx` is recorded instead of run: no plugin is needed to set a clone up."""
    calls: list = field(default_factory=list)

    def ctx(self, repo, *args, code=0):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")


def test_the_task_file_covers_each_kind_and_every_task_has_a_solution():
    assert 8 <= len(REAL_TASKS) <= 12
    assert {t.kind for t in REAL_TASKS} == bm.KINDS
    for t in REAL_TASKS:
        assert (bm.BENCH / "solutions" / f"{t.id}.patch").is_file(), t.id
        assert t.check == f"checks/{t.id}.py"
        assert "ctx" not in t.prompt.split(), f"{t.id}: the prompt names ctx, so the two arms differ"


def tasks_file(tmp_path: Path, body: str, checks: dict[str, str] | None = None) -> Path:
    for name, text in (checks if checks is not None else {"a.py": CHECK}).items():
        (tmp_path / "checks").mkdir(exist_ok=True)
        (tmp_path / "checks" / name).write_text(text)
    path = tmp_path / "tasks.toml"
    path.write_text(body)
    return path


def entry(id="a", kind="where", check="checks/a.py", **extra) -> str:
    lines = [f'id = "{id}"', f'kind = "{kind}"', 'prompt = "Do it."', f'check = "{check}"']
    return "[[task]]\n" + "\n".join(lines + [f'{k} = "{v}"' for k, v in extra.items()]) + "\n"


@pytest.mark.parametrize(("body", "checks", "message"), [
    ("", None, "holds no tasks"),
    (entry() + entry(), None, "duplicate task ids"),
    (entry(branch="x"), None, "unknown fields ['branch']"),
    ("[[task]]\nid = \"a\"\nkind = \"where\"\ncheck = \"checks/a.py\"\n", None, "lacks ['prompt']"),
    (entry(kind="feature"), None, "has kind 'feature'"),
    (entry(id="a,b"), None, "no commas"),
    (entry(check="checks/missing.py"), None, "no check file checks/missing.py"),
    (entry(check="../outside.py"), None, "no check file ../outside.py"),
    (entry(), {"a.py": "def broken(:\n"}, "does not compile"),
    (entry(), {"a.py": "print('no tests')\n"}, "not a runnable unittest file"),
])
def test_an_invalid_task_file_is_refused(tmp_path, body, checks, message):
    (tmp_path / "outside.py").write_text(CHECK)
    root = tmp_path / "bench"
    root.mkdir()
    with pytest.raises(Fail, match=message.replace("[", r"\[").replace("]", r"\]")):
        bm.load_tasks(tasks_file(root, body, checks))


def test_a_valid_task_file_loads_and_select_keeps_its_order(tmp_path):
    tasks = bm.load_tasks(tasks_file(tmp_path, entry("a") + entry("b", kind="history")))
    assert [(t.id, t.kind) for t in tasks] == [("a", "where"), ("b", "history")]
    assert [t.id for t in bm.select(tasks, "b, a")] == ["a", "b"]
    assert bm.select(tasks, None) == tasks
    with pytest.raises(Fail, match="unknown task"):
        bm.select(tasks, "a,zz")


def test_the_prompt_tells_the_session_it_is_unattended_and_not_to_commit():
    text = bm.prompt(REAL_TASKS[0])
    assert text.startswith(REAL_TASKS[0].prompt) and "Do not commit." in text


@pytest.mark.parametrize("arm", bm.ARMS)
def test_prepare_commits_the_arms_switch_over_the_story_with_its_corpus(tmp_path, arm):
    acc = FakeAcc(tmp_path, None, None, 1.0, ENV)
    repo = tmp_path / "clone"
    head = bm.prepare(acc, STORY, repo, arm)
    assert head == run_git(repo, "rev-parse", "HEAD~1").strip()
    assert run_git(repo, "log", "-1", "--format=%s").strip() == f"Benchmark arm: retrieval {arm}"
    config = run_git(repo, "show", "HEAD:.wscontext/config.toml")
    assert f"enabled = {'true' if arm == 'on' else 'false'}" in config.split("[retrieval]")[1].split("[")[0]
    assert run_git(repo, "status", "--porcelain") == ""
    assert run_git(repo, "remote").strip() == ""
    assert {"feature-budgets", "feature-report", "main"} <= set(run_git(repo, "branch", "--format=%(refname:short)").split())
    store = StorePaths.resolve(repo).local
    corpus = STORY.store
    for name in ("raw", "records"):
        wanted = sorted(p.relative_to(corpus).as_posix() for p in (corpus / name).rglob("*") if p.is_file())
        assert sorted(p.relative_to(store).as_posix() for p in (store / name).rglob("*") if p.is_file()) == wanted
    assert acc.calls == [("init",), ("rebuild",)]


@pytest.mark.parametrize("task", REAL_TASKS, ids=[t.id for t in REAL_TASKS])
def test_each_check_fails_on_the_untouched_story_and_passes_with_its_solution(tmp_path, task, capsys):
    acc = Acc(tmp_path, None, None, 0, ENV)
    assert bm.verify_checks(acc, STORY, [task]) == [], capsys.readouterr().out


def test_a_failing_story_suite_makes_a_task_not_done_even_when_its_check_passes(tmp_path):
    acc = Acc(tmp_path, None, None, 0, ENV)
    task = next(t for t in REAL_TASKS if t.id == "sqlite-timings")
    repo = tmp_path / "clone"
    bm.clone_subject(acc, STORY, repo)
    (repo / "SQLITE_TIMINGS.txt").write_text("44.55\n3.62\n")
    (repo / "tests" / "test_amount.py").write_text("import unittest\n\n\nclass T(unittest.TestCase):\n"
                                                   "    def test_x(self):\n        self.fail('broken')\n")
    found = bm.run_check(acc, STORY, repo, bm.BENCH / task.check)
    assert (found["check"], found["suite"], found["done"]) == (True, False, False)
    assert "broken" in found["suite_output"] and "check_output" not in found


def test_figures_come_from_the_real_stats_session_output(git_repo, monkeypatch, capsys, spawned):
    monkeypatch.chdir(git_repo)
    store = StorePaths.resolve(git_repo)
    sid = "bench-1"
    write_session(store, SessionRecord(
        session_id=sid, author="t@example.com", branch="main", started_at="2026-11-11T10:00:00Z",
        ended_at="2026-11-11T10:03:00Z", ended_by="session-end", retrieval=True,
        tool_calls={"search": 3, "read": 5, "edit": 2, "shell": 4, "ctx": 2, "agent": 1},
        ctx_calls={"code": 1, "find": 2},
        tokens={"input": 100, "output": 50, "cache_read": 1000, "cache_creation": 200},
        turns=[TurnRecord(session_id=sid, author="t@example.com", index=0, started_at="2026-11-11T10:00:00Z",
                          ended_at="2026-11-11T10:02:30Z", tool_calls={"read": 5},
                          tokens={"input": 100, "output": 50, "cache_read": 1000, "cache_creation": 200})]),
        Config())
    capsys.readouterr()
    assert ctx_main(["--json", "--full", "stats", "session", sid]) == 0
    results = json.loads(capsys.readouterr().out)["results"]
    found = bm.figures(results)
    assert {k: found[k] for k in ("prompts", "tool_calls", "search", "read", "edit", "shell", "other", "ctx",
                                  "tokens", "output_tokens", "seconds", "ended_by", "retrieval")} == {
        "prompts": 1, "tool_calls": 17, "search": 3, "read": 5, "edit": 2, "shell": 4, "other": 3, "ctx": 3,
        "tokens": 1350, "output_tokens": 50, "seconds": 150.0, "ended_by": "session-end", "retrieval": True}
    assert found["ctx_calls"] == {"code": 1, "find": 2}


@dataclass
class SessionAcc(FakeAcc):
    """claude does the task by applying its solution, or times out; the stats are canned."""
    timeout: bool = False

    def claude_start(self, repo, prompt, session=None, resume=False, tools=""):
        self.calls.append(("claude", prompt, tools))
        run = self.tmp / f"{session}-run"
        Path(f"{run}.out").write_text(json.dumps({"type": "result", "subtype": "success", "num_turns": 9,
                                                  "total_cost_usd": 0.31, "duration_ms": 30000}) + "\n")
        task = next(t for t in REAL_TASKS if prompt.startswith(t.prompt))
        if not self.timeout:
            run_git(repo, "apply", str(bm.BENCH / "solutions" / f"{task.id}.patch"))
        return session, None, str(run)

    def claude_finish(self, sid, proc, run):
        if self.timeout:
            raise Fail("claude -p ran longer than 300s")
        return Claude(sid, False, "Done.", ["Edit"])

    def ctx_json(self, repo, *args, code=0):
        self.calls.append(args)
        return {"results": [{"ended_by": "session-end", "tool_calls": {"edit": 2, "search": 1}, "ctx_calls": {},
                             "tokens": {"input": 10, "output": 5}},
                            {"turns": [{"seconds": 25.0}]}]}


def test_a_session_runs_its_check_after_it_ends_and_records_the_figures(tmp_path):
    acc = SessionAcc(tmp_path, None, None, 1.0, ENV)
    task = next(t for t in REAL_TASKS if t.id == "bar-width")
    found = bm.run_session(acc, STORY, task, "on", 1)
    assert (found["done"], found["error"], found["turns"], found["cost_usd"], found["tool_calls"],
            found["search"], found["tokens"], found["seconds"]) == (True, None, 9, 0.31, 3, 1, 15, 25.0)
    claude = next(c for c in acc.calls if c[0] == "claude")
    assert claude[1] == bm.prompt(task) and claude[2] == bm.BENCH_TOOLS == STORY.tools_allowed


def test_a_session_that_times_out_is_not_done_and_says_why(tmp_path):
    acc = SessionAcc(tmp_path, None, None, 1.0, ENV, timeout=True)
    task = next(t for t in REAL_TASKS if t.id == "warning-origin")
    found = bm.run_session(acc, STORY, task, "off", 1)
    assert found["done"] is False and found["error"] == "claude: claude -p ran longer than 300s"


def test_claude_result_reads_model_turns_and_cost(tmp_path):
    run = tmp_path / "run"
    Path(f"{run}.out").write_text("\n".join([
        json.dumps({"type": "system", "subtype": "init", "model": "claude-test"}),
        "not json",
        json.dumps({"type": "result", "subtype": "success", "num_turns": 7, "total_cost_usd": 0.42,
                    "duration_ms": 61500})]) + "\n")
    assert bm.claude_result(str(run)) == {"model": "claude-test", "turns": 7, "cost_usd": 0.42,
                                          "wall_seconds": 61.5, "result": "success"}
    assert bm.claude_result(None)["turns"] is None


def row(task, arm, done, run=1, **figures):
    base = {key: None for key, _ in bm.FIGURES}
    return {"task": task, "kind": "where", "arm": arm, "run": run, "session": f"{task}-{arm}-{run}",
            "done": done, "check": done, "suite": True, "error": None, **base, **figures}


ROWS = [row("a", "off", True, turns=10, tool_calls=20, search=6, read=8, ctx=0, tokens=1000, seconds=60, cost_usd=0.5),
        row("a", "on", True, turns=6, tool_calls=12, search=1, read=4, ctx=3, tokens=800, seconds=40, cost_usd=0.3),
        row("b", "off", False, turns=4, tool_calls=5, search=2, read=2, ctx=0, tokens=300, seconds=20, cost_usd=0.1),
        row("b", "on", True, turns=8, tool_calls=9, search=0, read=3, ctx=2, tokens=900, seconds=50, cost_usd=0.4)]
META = {"date": "2026-11-11T12:00:00+00:00", "plugin_version": "0.3.0", "plugin_root": "/p", "model": "m",
        "runs": 1, "tasks": ["a", "b"], "arms": list(bm.ARMS), "max_budget_usd": 2.0,
        "story": "eval/stage2/repo.bundle", "story_head": "78ccd84abc", "complete": True}


def test_the_report_puts_done_beside_every_cost_and_compares_tasks_done_in_both_arms(tmp_path):
    doc = bm.report(ROWS, META)
    summary = doc["summary"]
    assert summary["total"]["off"]["done"] == 1 and summary["total"]["off"]["runs"] == 2
    assert summary["total"]["off"]["cost_usd"] == pytest.approx(0.6)
    assert summary["total"]["on"]["tool_calls"] == 21
    assert summary["done_in_both"]["tasks"] == ["a"]
    assert summary["done_in_both"]["off"]["tokens"] == 1000 and summary["done_in_both"]["on"]["tokens"] == 800
    text = bm.markdown(doc)
    assert "| off | 1/2 | 14 | 25 | 8 | 10 |" in text
    assert "| b | off | 0/1 | 4 | 5 |" in text
    assert "- b, off, run 1 (session b-off-1): check failed" in text
    assert "Tasks done in every run of both arms (1: a)" in text
    json_path, md_path = bm.write_report(doc, tmp_path)
    assert (json_path.name, md_path.name) == ("2026-11-11-0.3.0.json", "2026-11-11-0.3.0.md")
    assert json.loads(json_path.read_text())["runs"] == ROWS
    assert bm.write_report(doc, tmp_path)[0].name == "2026-11-11-0.3.0-2.json"


def test_figures_are_means_over_runs():
    rows = [row("a", "on", True, run=1, turns=4, cost_usd=0.2), row("a", "on", False, run=2, turns=8, cost_usd=None)]
    found = bm.aggregate(rows)
    assert (found["runs"], found["done"], found["turns"], found["cost_usd"], found["tokens"]) == (2, 1, 6, 0.2, None)


def test_the_dry_run_prints_the_plan_and_calls_nothing(monkeypatch, capsys):
    def refuse(*a, **k):
        raise AssertionError(f"the dry run started a process: {a}")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    assert bm.main(["--dry-run", "--tasks", "bar-width,warning-origin", "--runs", "2", "--max-budget-usd", "1.5"]) == 0
    out = capsys.readouterr().out
    assert "2 task(s) x 2 arms (off then on) x 2 run(s) = 8 claude -p session(s)" in out
    assert "$1.50 per session, $12.00 at most in total (an upper bound, not an estimate)" in out
    assert "bar-width" in out and "warning-origin" in out and "category-length" not in out
    command = next(line for line in out.splitlines() if line.startswith("  claude -p"))
    for part in ("--max-budget-usd 1.5", "--setting-sources project,local", "--strict-mcp-config",
                 "Grep Glob", "Bash(ctx:*)", "--permission-mode dontAsk"):
        assert part in command
    assert out.rstrip().endswith("nothing run")


def test_an_unknown_task_stops_before_anything_runs(capsys):
    assert bm.main(["--dry-run", "--tasks", "nope"]) == 1
    assert "unknown task(s): nope" in capsys.readouterr().err


def test_a_clone_keeps_every_corpus_session_when_the_clock_is_two_years_ahead(tmp_path, monkeypatch):
    # The setup copies the corpus into the store without committing it to the history ref, and
    # pruning only removes a copy whose blob is on the history tip, so the 180-day retention never
    # reaches it. A history tip with something else on it makes the prune pass do its full check.
    acc = FakeAcc(tmp_path, None, None, 1.0, ENV)
    repo = tmp_path / "clone"
    bm.prepare(acc, STORY, repo, "on")
    store = StorePaths.resolve(repo)
    other = store.raw / "sessions" / "2026-01-01-other.jsonl"
    other.write_text("{}\n")
    assert commit_paths(store, [other], "unrelated") is not None
    before = sorted(p for p in store.raw.rglob("*") if p.is_file())
    assert len(before) > 1
    later = time.time() + 2 * 365 * 86400
    monkeypatch.setattr(worker.time, "time", lambda: later)
    assert worker._prune(store, load_config(repo)) == []
    assert sorted(p for p in store.raw.rglob("*") if p.is_file()) == before
    assert len(list((store.records / "sessions").glob("*.json"))) == len(
        list((STORY.store / "records" / "sessions").glob("*.json")))


def test_the_story_subject_is_the_stage_2_clone_with_its_corpus_and_the_runners_interpreter():
    assert (STORY.bundle.name, STORY.branch, STORY.store.name, STORY.setup, STORY.tools) == (
        "repo.bundle", "main", "corpus", "none", "")
    assert STORY.bundle.parent == STORY.store.parent and STORY.bundle.parent.name == "stage2"
    assert STORY.suite == "python -m unittest discover -s tests"


def subject_file(folder: Path, **fields) -> Path:
    base = {"bundle": "b.bundle", "branch": "main", "store": "store", "suite": "python -m unittest",
            "setup": "none", **fields}
    folder.mkdir(exist_ok=True)
    (folder / "subject.toml").write_text("".join(f'{k} = "{v}"\n' for k, v in base.items()))
    return folder


def test_a_subject_file_loads_with_its_optional_tools(tmp_path):
    subject = bm.load_subject(subject_file(tmp_path, setup="uv-venv", tools="Bash(.venv/bin/pytest:*)"))
    assert (subject.setup, subject.bundle, subject.store) == ("uv-venv", tmp_path / "b.bundle", tmp_path / "store")
    assert subject.tools_allowed == bm.BENCH_TOOLS + " Bash(.venv/bin/pytest:*)"


@pytest.mark.parametrize(("fields", "message"), [
    ({"extra": "x"}, r"unknown fields \['extra'\]"),
    ({"setup": "pip"}, "setup is 'pip'"),
    ({"suite": " "}, "suite is empty"),
])
def test_an_invalid_subject_file_is_refused(tmp_path, fields, message):
    with pytest.raises(Fail, match=message):
        bm.load_subject(subject_file(tmp_path, **fields))


def test_a_subject_file_without_a_required_field_is_refused(tmp_path):
    (tmp_path / "subject.toml").write_text('bundle = "b"\nbranch = "main"\n')
    with pytest.raises(Fail, match=r"lacks \['setup', 'store', 'suite'\]"):
        bm.load_subject(tmp_path)


def test_a_subject_with_a_venv_runs_its_check_and_suite_with_the_clones_interpreter(tmp_path):
    ran = []

    @dataclass
    class Recording(FakeAcc):
        def run(self, cmd, cwd, stdin=None, timeout=120, env=None):
            ran.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

    acc = Recording(tmp_path, None, None, 1.0, ENV)
    repo = tmp_path / "clone"
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("")
    source = tmp_path / "check.py"
    source.write_text(CHECK)
    subject = bm.Subject(tmp_path, tmp_path, "main", tmp_path, "python -m pytest -q", "uv-venv", "")
    assert bm.run_check(acc, subject, repo, source)["done"] is True
    python = str(repo / ".venv" / "bin" / "python")
    assert ran == [[python, bm.CHECK_FILE], [python, "-m", "pytest", "-q"]]
    ran.clear()
    (repo / ".venv" / "bin" / "python").unlink()
    bm.run_check(acc, subject, repo, source)
    assert ran == [[sys.executable, bm.CHECK_FILE], [sys.executable, "-m", "pytest", "-q"]]


def test_the_uv_venv_setup_builds_a_venv_in_the_clone_and_keeps_it_out_of_the_commits(tmp_path, monkeypatch):
    ran = []

    @dataclass
    class Recording(Acc):
        def run(self, cmd, cwd, stdin=None, timeout=120, env=None):
            if cmd[0] != "uv":
                return super().run(cmd, cwd, stdin, timeout, env)
            ran.append((cmd, cwd))
            return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(bm.shutil, "which", lambda name: "/bin/uv")
    acc = Recording(tmp_path, None, None, 1.0, ENV)
    repo = tmp_path / "clone"
    bm.clone_subject(acc, bm.Subject(STORY.dir, STORY.bundle, "main", STORY.store, STORY.suite, "uv-venv", ""), repo)
    assert [c[0][:3] for c in ran] == [["uv", "venv", "-q"], ["uv", "pip", "install"]]
    assert ran[0][0][3:5] == ["--python", sys.executable]
    assert all(cwd == repo for _, cwd in ran) and "-e" in ran[1][0] and "dev" in ran[1][0]
    (repo / ".venv").mkdir()
    (repo / ".venv" / "x").write_text("")
    assert run_git(repo, "status", "--porcelain") == ""


def absent_subject(tmp_path: Path):
    """A one-commit subject whose file says 'in-the-tree' and whose commit message says 'in-the-log'."""
    src = tmp_path / "src"
    src.mkdir()
    run_git(src, "init", "-q", "-b", "main")
    run_git(src, "config", "user.name", "t")
    run_git(src, "config", "user.email", "t@example.invalid")
    (src / "f.txt").write_text("in-the-tree\n")
    run_git(src, "add", "f.txt")
    run_git(src, "commit", "-q", "-m", "in-the-log")
    folder = tmp_path / "subject"
    (folder / "store" / "records").mkdir(parents=True)
    run_git(src, "bundle", "create", str(folder / "b.bundle"), "main")
    subject_file(folder, suite="python -c pass")
    (folder / "solutions").mkdir()
    (folder / "solutions" / "a.patch").write_text(
        "diff --git a/g.txt b/g.txt\nnew file mode 100644\n--- /dev/null\n+++ b/g.txt\n@@ -0,0 +1 @@\n+x\n")
    (folder / "checks").mkdir()
    (folder / "checks" / "a.py").write_text(
        "import pathlib, unittest\n\n\nclass T(unittest.TestCase):\n    def test_x(self):\n"
        "        self.assertTrue(pathlib.Path('g.txt').exists())\n\n\nunittest.main()\n")
    return bm.load_subject(folder), folder


@pytest.mark.parametrize(("absent", "where"), [
    ([], None), (["nowhere"], None), (["in-the-tree"], "tracked files"), (["in-the-log"], "commit messages")])
def test_an_absent_string_in_the_tree_or_the_log_fails_verify_checks(tmp_path, absent, where, capsys):
    subject, folder = absent_subject(tmp_path)
    (folder / "tasks.toml").write_text(entry() + "absent = " + json.dumps(absent) + "\n")
    task = bm.load_tasks(folder / "tasks.toml")[0]
    assert task.absent == tuple(absent)
    (tmp_path / "work").mkdir()
    problems = bm.verify_checks(Acc(tmp_path / "work", None, None, 0, ENV), subject, [task])
    assert problems == ([] if where is None else [f"a: the answer string {absent[0]!r} is in the {where}"])
    capsys.readouterr()


def test_absent_must_be_a_list_of_strings(tmp_path):
    with pytest.raises(Fail, match="absent must be a list of non-empty strings"):
        bm.load_tasks(tasks_file(tmp_path, entry(absent="plain")))


def test_the_dry_run_prints_the_subject_and_the_report_goes_to_its_runs_folder(capsys):
    assert bm.main(["--dry-run", "--tasks", "bar-width"]) == 0
    out = capsys.readouterr().out
    for line in ("bundle: eval/stage2/repo.bundle at main", "suite: python -m unittest discover -s tests",
                 "setup: none", "extra tools: none", "recorded sessions from eval/stage2/corpus",
                 "report: eval/bench/runs/"):
        assert line in out
