from support import hook, transcript  # noqa: F401  (fixtures)
import io
import json
import subprocess
import sys
import time

from wscontext.capture import dispatch, handlers
from wscontext.capture.kinds import ctx_invocations, tool_kind
from wscontext.capture.worker import detach_kwargs


def test_raising_handler_exits_zero_and_logs(hook, monkeypatch, capsys):
    def boom(store, config, payload):
        raise RuntimeError("boom")

    monkeypatch.setitem(handlers.HANDLERS, "stop", boom)
    assert hook("Stop", "stop")["ok"] is False
    line = json.loads((hook.store.logs / "hooks.jsonl").read_text().splitlines()[0])
    assert line["ok"] is False and "RuntimeError: boom" in line["error"]
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert dispatch.main(["Stop"]) == 0


def test_bad_input_exits_zero(monkeypatch):
    for argv, stdin in [(["Stop"], "not json"), (["Nope"], "{}"), ([], ""), (["Stop"], "[]")]:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        assert dispatch.main(argv) == 0


def test_unknown_event_logged(hook):
    entry = hook("Nope", "stop")
    assert entry["ok"] is False and "unknown hook event" in entry["error"]


def test_slow_handler_stops_at_time_budget(hook, git_repo, monkeypatch):
    (git_repo / ".wscontext").mkdir()
    (git_repo / ".wscontext" / "config.toml").write_text("[hooks]\ntime_budget_ms = 100\n")
    monkeypatch.setitem(handlers.HANDLERS, "stop", lambda s, c, p: time.sleep(3))
    started = time.monotonic()
    entry = hook("Stop", "stop")
    assert time.monotonic() - started < 2
    assert entry["ok"] is False and "timeout" in entry["error"]
    assert "timeout" in (hook.store.logs / "hooks.jsonl").read_text()


def test_event_name_forms(hook):
    assert hook("session-start", "session_start")["ok"]
    assert hook("user_prompt_submit", "user_prompt_submit")["ok"]


def test_ctx_detection():
    assert ctx_invocations("ctx why a.py:3") == [("why", ["why", "a.py:3"])]
    assert ctx_invocations("cd x && /usr/local/bin/ctx stats --json | head")[0][0] == "stats"
    assert ctx_invocations("echo ctx why") == []
    assert ctx_invocations("grep -r ctx src") == []
    assert tool_kind("Bash", "ctx show abc") == "ctx"
    assert tool_kind("Bash", "ls") == "shell"
    assert tool_kind("Grep") == "search" and tool_kind("Read") == "read"
    assert tool_kind("mcp__x__y") == "mcp" and tool_kind("Whatever") == "other"


def test_detach_kwargs_per_platform():
    assert detach_kwargs("linux") == {"start_new_session": True}
    assert detach_kwargs("darwin") == {"start_new_session": True}
    assert detach_kwargs("win32") == {"creationflags": 0x00000008 | 0x00000200}


def test_hook_path_imports_only_stdlib(tmp_path):
    code = ("import sys\nbefore = set(sys.modules)\n"
            "import wscontext.capture.dispatch, wscontext.capture.worker, "
            "wscontext.history.sync, wscontext.redact.redactor, wscontext.logs, "
            "wscontext.cli.main, wscontext.githooks.dispatch\n"
            "new = {m.split('.')[0] for m in set(sys.modules) - before}\n"
            "print(sorted(new - set(sys.stdlib_module_names) - {'wscontext'}))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]", out.stdout
    # `ctx hook` and `ctx git-hook` through the entry point, from a directory that is no repository.
    for argv in (["hook", "Stop"], ["git-hook", "post-checkout"], ["worker", "copy"]):
        run = ("import io, sys\nsys.stdin = io.StringIO('')\n"
               "before = set(sys.modules)\n"
               "from wscontext.cli.main import main\n"
               f"assert main({argv!r}) == 0\n"
               "new = set(sys.modules) - before\n"
               "third = {m.split('.')[0] for m in new} - set(sys.stdlib_module_names) - {'wscontext'}\n"
               "# cli.output is the one cli module the hook path reuses (the brief's frame)\n"
               "cli = sorted(m for m in new if m.startswith('wscontext.cli.') and m not in {'wscontext.cli.main', 'wscontext.cli.output'})\n"
               "print(sorted(third), cli)")
        done = subprocess.run([sys.executable, "-c", run], capture_output=True, text=True,
                              cwd=tmp_path, check=True)
        assert done.stdout.strip() == "[] []", (argv, done.stdout, done.stderr)


def test_real_subprocess_hook_spawns_detached_worker(git_repo, transcript, tmp_path):
    stdin = json.dumps({"session_id": "sess-cap1", "transcript_path": str(transcript),
                        "cwd": str(git_repo)})
    done = subprocess.run([sys.executable, "-m", "wscontext.capture.dispatch", "Stop"],
                          input=stdin, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0
    from wscontext.store.paths import StorePaths
    store = StorePaths.resolve(git_repo)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        copies = list((store.raw / "sessions").glob("*-sess-cap1.jsonl"))
        if copies and b"ghp_" not in copies[0].read_bytes():
            log = (store.logs / "hooks.jsonl").read_text() if (store.logs / "hooks.jsonl").exists() else ""
            if "worker:copy" in log:
                break
        time.sleep(0.1)
    else:
        raise AssertionError("worker did not finish")
