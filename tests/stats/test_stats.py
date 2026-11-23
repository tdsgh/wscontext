import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The stats tests reuse the CLI tests' world fixture.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))

from cli_support import add_session, checkpoint, run_json, spawned, world  # noqa: F401

from wscontext.cli.main import main
from wscontext.config import Config, SyncConfig
from wscontext.history.branch import commit_paths
from wscontext.store.paths import StorePaths
from wscontext.store.records import SessionRecord, TurnRecord, write_session


def log(store, name: str, *rows: dict, generation: str = "") -> None:
    store.logs.mkdir(parents=True, exist_ok=True)
    with (store.logs / (name + generation)).open("a") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)


def stamp(minutes_ago: float = 0, seconds_ago: float = 0) -> str:
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago, seconds=seconds_ago)
    return when.isoformat(timespec="seconds")


def stats(world, capsys, *argv: str) -> dict:
    return run_json(world, capsys, "stats", *argv)["results"]


def test_empty_store_prints_zeros_not_an_error(git_repo, monkeypatch, capsys):
    monkeypatch.chdir(git_repo)
    for view in ("data", "turns", "queries", "hooks"):
        assert main(["stats", view]) == 0
        out = capsys.readouterr().out
        assert "RETRIEVED DATA" in out and "Traceback" not in out
    main(["stats", "data", "--json"])
    data = json.loads(capsys.readouterr().out)["results"][0]
    assert (data["sessions"], data["turns"], data["linked"], data["store_bytes"],
            data["branch_bytes"], data["redaction_hits"]) == (0, 0, 0, 0, 0, 0)
    assert data["commits"] == 1 and data["linked_share"] == 0
    main(["stats", "queries", "--json"])
    queries = json.loads(capsys.readouterr().out)["results"][0]
    assert queries["calls"] == 0 and queries["ctx_first_share"] is None


def test_data_view_golden(world, capsys):
    checkpoint(world.store, world.c1, ["sess-one"])
    log(world.store, "hooks.jsonl",
        {"ts": stamp(), "event": "worker:copy", "ok": True, "redactions": 2, "ms": 5},
        {"ts": stamp(), "event": "worker:final", "ok": True, "redactions": 1, "ms": 5},
        {"ts": stamp(), "event": "worker:fetch", "ok": True, "ms": 5})
    code, out, _ = world.ctx(capsys, "stats", "data")
    assert code == 0
    lines = out.splitlines()
    assert lines[2] == "[stats data] collected data"
    assert lines[3].startswith("  sessions: 1; turns: 2; transcript events: ")
    assert lines[4] == "  commits: 3; linked to a session: 1 (33%)"
    assert lines[6] == "  redaction hits: 3 across 2 transcript copies"
    assert lines[7] == "  sharing: off (no sync remote set)"
    data = stats(world, capsys, "data")[0]
    assert data["store_bytes"] > 0 and data["branch_bytes"] > 0
    assert data["sync"]["sharing"] is False


def test_data_window_and_bad_since(world, capsys):
    assert stats(world, capsys, "data", "--since", "2026-06-01")[0]["sessions"] == 0
    assert stats(world, capsys, "data", "--since", "2026-01-01")[0]["sessions"] == 1
    code, _, err = world.ctx(capsys, "stats", "--since", "soon")
    assert code == 1 and "not a date or duration" in err


def test_data_reports_sync_status_and_starts_the_fetch(shared_remote, capsys, monkeypatch, spawned):
    from wscontext.history import sync
    _, a, _ = shared_remote
    monkeypatch.chdir(a)
    (a / ".wscontext").mkdir()
    (a / ".wscontext" / "config.toml").write_text('[sync]\nremote = "origin"\n')
    store = StorePaths.resolve(a)
    commit_paths(store, add_session(store, "sess-one"), "one")
    sync.push(store, Config(sync=SyncConfig(remote="origin")))
    commit_paths(store, add_session(store, "sess-two", started="2026-02-01T10:00:00Z"), "two")
    capsys.readouterr()
    assert main(["stats", "data", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)["results"][0]
    assert data["sync"] == {"sharing": True, "remote": "origin", "ahead": 1, "behind": 0,
                            "pending_sessions": 1}
    assert [c[0] for c in spawned] == ["fetch", "sweep"]


def test_turns_view(world, capsys):
    write_session(world.store, SessionRecord(
        session_id="sess-timed", author="t@example.com", started_at="2026-01-02T10:00:00Z",
        tool_calls={"search": 1, "ctx": 2}, tokens={"input": 7},
        turns=[TurnRecord(session_id="sess-timed", author="t", index=i,
                          started_at="2026-01-02T10:00:00Z", ended_at=end)
               for i, end in enumerate(["2026-01-02T10:00:04Z", "2026-01-02T10:00:20Z", None])]),
        Config())
    t = stats(world, capsys, "turns")[0]
    assert t["sessions"] == 2 and t["turns"] == 5 and t["timed_turns"] == 2
    assert (t["turn_seconds_p50"], t["turn_seconds_p95"]) == (4, 20)
    assert t["tool_calls"] == {"read": 2, "edit": 1, "search": 1, "ctx": 2}
    assert t["tokens"] == {"input": 7}
    _, out, _ = world.ctx(capsys, "stats", "turns")
    assert "tool calls: ctx 2, edit 1, read 2, search 1" in out
    assert "sess-timed  2026-01-02  3      24s   ctx 2, search 1" in out


def test_queries_join_calls_to_sessions_and_find_ctx_first(world, capsys):
    store = world.store
    # sess-ctx opens with a ctx call; sess-one (the fixture) starts with a Read.
    add_session(store, "sess-ctx", transcript=False, started="2026-01-03T10:00:00Z")
    raw = store.raw / "sessions" / "2026-01-03-sess-ctx.jsonl"
    raw.write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "version": "2.0.5", "timestamp": "2026-01-03T10:00:00Z",
         "message": {"role": "user", "content": "why is it so"}},
        {"type": "assistant", "version": "2.0.5", "timestamp": "2026-01-03T10:00:01Z",
         "message": {"id": "m1", "role": "assistant", "content": [
             {"type": "tool_use", "id": "t1", "name": "Bash",
              "input": {"command": "ctx why src/app.py"}},
             {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "a"}}]}},
    ]) + "\n")
    now = stamp()
    log(store, "ctx-calls.jsonl",
        {"ts": now, "source": "cli", "argv": ["why", "src/app.py"], "exit": 0, "ms": 40,
         "output_bytes": 900, "results": 2, "flags": ["uncommitted"]},
        {"ts": now, "source": "hook", "session_id": "sess-ctx", "argv": ["why", "src/app.py"],
         "output_bytes": 900},
        # quoted in the shell: the hook sees the quote characters
        {"ts": now, "source": "cli", "argv": ["sessions", "--file", "a b"], "exit": 0, "ms": 10,
         "output_bytes": 100, "results": 1, "flags": []},
        {"ts": now, "source": "hook", "session_id": "sess-one", "argv": ["sessions", "--file", '"a', 'b"']},
        # a person's call in a terminal: no hook line, no session
        {"ts": now, "source": "cli", "argv": ["why", "x.py"], "exit": 1, "ms": 90,
         "output_bytes": 50, "results": None, "flags": []},
        # reading the report is not a query
        {"ts": now, "source": "cli", "argv": ["stats", "queries"], "exit": 0, "ms": 1,
         "output_bytes": 1})
    # an older generation, outside the join window of the live one
    log(store, "ctx-calls.jsonl",
        {"ts": stamp(minutes_ago=30), "source": "cli", "argv": ["why", "old.py"], "exit": 0,
         "ms": 7, "output_bytes": 10, "results": 0, "flags": []}, generation=".1")
    q = stats(world, capsys, "queries")
    summary, calls = q[0], q[1]["calls"]
    assert summary["calls"] == 4 and summary["from_sessions"] == 2 and summary["failed"] == 1
    assert summary["by_command"] == {"why": 3, "sessions": 1}
    assert summary["flags"] == {"uncommitted": 1}
    assert summary["results_total"] == 3 and summary["ms_p50"] == 10
    assert [(c["query"], c["session_id"]) for c in calls] == [
        ("old.py", None), ("src/app.py", "sess-ctx"), ("a b", "sess-one"), ("x.py", None)]
    assert (summary["sessions_considered"], summary["ctx_first"]) == (2, 1)
    assert summary["ctx_first_share"] == 0.5
    assert [c["query"] for c in stats(world, capsys, "queries", "--since", "10m")[1]["calls"]] == [
        "src/app.py", "a b", "x.py"]
    _, out, _ = world.ctx(capsys, "stats", "queries")
    assert "ctx before first search or read: 1 of 2 sessions (50%)" in out
    assert "sess-ctx" in out


def test_join_respects_the_time_window(world, capsys):
    log(world.store, "ctx-calls.jsonl",
        {"ts": stamp(minutes_ago=10), "source": "cli", "argv": ["why", "a.py"], "exit": 0,
         "ms": 1, "output_bytes": 1},
        {"ts": stamp(), "source": "hook", "session_id": "sess-one", "argv": ["why", "a.py"]})
    calls = stats(world, capsys, "queries")[1]["calls"]
    assert [c["session_id"] for c in calls] == [None, "sess-one"]
    assert calls[1]["ms"] is None  # the hook line stands alone


def test_hooks_view(world, capsys):
    rows = [{"ts": stamp(), "event": "Stop", "ok": True, "ms": ms} for ms in range(10, 110, 10)]
    rows.append({"ts": stamp(), "event": "Stop", "ok": False, "ms": 2000,
                 "error": "timeout after 2000 ms", "session_id": "sess-one"})
    log(world.store, "hooks.jsonl", {"ts": stamp(minutes_ago=5), "event": "SessionStart",
                                     "ok": True, "ms": 12}, generation=".1")
    log(world.store, "hooks.jsonl", *rows)
    h = stats(world, capsys, "hooks")
    assert h[0]["runs"] == 12 and h[0]["failures"] == 1
    by = {e["event"]: e for e in h[0]["events"]}
    assert by["Stop"] == {"event": "Stop", "runs": 11, "failures": 1, "p50_ms": 60, "p95_ms": 2000}
    assert by["SessionStart"]["runs"] == 1
    assert h[1]["recent_failures"][0]["error"] == "timeout after 2000 ms"
    _, out, _ = world.ctx(capsys, "stats", "hooks")
    assert "Stop          11" in out and "recent failures" in out


def test_session_view_and_errors(world, capsys):
    log(world.store, "ctx-calls.jsonl",
        {"ts": stamp(), "source": "cli", "argv": ["why", "src/app.py"], "exit": 0, "ms": 5,
         "output_bytes": 10, "results": 1, "flags": []},
        {"ts": stamp(), "source": "hook", "session_id": "sess-one", "argv": ["why", "src/app.py"]})
    r = stats(world, capsys, "session", "sess-o")
    assert r[0]["id"] == "sess-one" and r[0]["first_tool"] == "read"
    assert [t["index"] for t in r[1]["turns"]] == [0, 1]
    assert [c["query"] for c in r[2]["calls"]] == ["src/app.py"]
    _, out, _ = world.ctx(capsys, "stats", "session", "sess-one")
    assert "first search, read or ctx call: read" in out
    add_session(world.store, "sess-two")
    code, _, err = world.ctx(capsys, "stats", "session", "sess")
    assert code == 1 and "sess-one, sess-two" in err
    assert world.ctx(capsys, "stats", "session", "nope")[0] == 1
    assert world.ctx(capsys, "stats", "session")[0] == 1


def test_code_view(world, capsys):
    _, out, _ = world.ctx(capsys, "stats", "code")
    assert "no graph yet" in out and "available from stage 3" not in out


def test_stats_writes_nothing_but_its_call_log(world, capsys):
    def files():
        return {p: p.stat().st_mtime_ns for p in world.store.local.rglob("*")
                if p.is_file() and p.parent != world.store.logs}

    before = files()
    for view in ("data", "turns", "queries", "hooks", "session sess-one"):
        world.ctx(capsys, "stats", *view.split())
    assert files() == before
    assert (world.store.logs / "ctx-calls.jsonl").exists()


def test_calls_log_result_counts_and_freshness_flags(world, capsys):
    world.ctx(capsys, "why", "src/app.py")
    world.ctx(capsys, "sessions")
    rows = [json.loads(x) for x in (world.store.logs / "ctx-calls.jsonl").read_text().splitlines()]
    assert (rows[0]["results"], rows[0]["flags"]) == (2, ["uncommitted"])
    assert (rows[1]["results"], rows[1]["flags"]) == (1, [])


def test_hook_row_with_a_redirection_joins_its_cli_row(world, capsys):
    now = stamp()
    log(world.store, "ctx-calls.jsonl",
        {"ts": now, "source": "cli", "argv": ["find", "a", "b"], "exit": 0, "ms": 50,
         "output_bytes": 400, "results": 3, "flags": []},
        {"ts": stamp(seconds_ago=1), "source": "hook", "session_id": "sess-one",
         "argv": ["find", "a", "b", "2>/dev/null"]},
        {"ts": now, "source": "cli", "argv": ["find", "c"], "exit": 0, "ms": 50,
         "output_bytes": 400, "results": 1, "flags": []},
        {"ts": now, "source": "hook", "session_id": "sess-one", "argv": ["find", "c", "2>"]})
    calls = stats(world, capsys, "queries")[1]["calls"]
    assert [(c["query"], c["session_id"], c["results"]) for c in calls] == [
        ("a b", "sess-one", 3), ("c", "sess-one", 1)]


def test_sessions_without_a_transcript_are_never_prompted(world, capsys):
    write_session(world.store, SessionRecord(
        session_id="sess-empty", author="t@example.com", started_at="2026-01-01T10:00:00Z"),
        Config())
    write_session(world.store, SessionRecord(
        session_id="sess-calls", author="t@example.com", started_at="2026-01-01T10:00:00Z",
        tool_calls={"read": 1}), Config())  # no turns, no stored bytes, but tool calls
    q = stats(world, capsys, "queries")[0]
    assert q["adoption"]["sessions"] == 2 and q["adoption"]["never_prompted"] == 1
    assert [w["sessions"] for w in q["adoption"]["by_week"]] == [2]
    _, out, _ = world.ctx(capsys, "stats", "queries")
    assert "sessions never prompted: 1" in out


def _prompted(world, sid, tokens=None, named=None, ctx=None, calls=None):
    write_session(world.store, SessionRecord(
        session_id=sid, author="t@example.com", started_at=stamp(), tool_calls={"read": 1},
        ctx_calls=ctx or {}, prompt_brief_tokens=tokens, prompt_brief_sessions=named), Config())


def test_data_view_reports_the_prompt_brief(world, capsys):
    _prompted(world, "p-one", 120, ["sess-one"])
    _prompted(world, "p-two", 80, ["sess-one"])
    _prompted(world, "p-none")  # prompted, no prompt brief
    write_session(world.store, SessionRecord(  # never prompted: not in the denominator
        session_id="p-empty", author="t@example.com", started_at=stamp()), Config())
    r = stats(world, capsys, "data")[-1]["retrieval"]
    # sess-one, from the fixture, has turns, so 4 sessions have a transcript
    assert (r["sessions_with_prompt_brief"], r["prompt_brief_tokens"]) == (2, 200)
    assert r["prompt_brief_share"] == 0.5
    _, out, _ = world.ctx(capsys, "stats", "data")
    assert "prompt brief: on; sessions given one: 2 (50% of sessions with a transcript); " \
           "prompt brief tokens given: 200" in out


def test_queries_view_reports_prompt_brief_follow_through(world, capsys):
    _prompted(world, "f-show", 100, ["a"], ctx={"show": 1})
    _prompted(world, "f-find", 100, ["a"], ctx={"find": 2})
    _prompted(world, "f-why", 100, ["a"], ctx={"why": 1})  # why is not a follow-up
    _prompted(world, "f-nobrief", None, ctx={"find": 1})  # no prompt brief: not counted
    p = stats(world, capsys, "queries")[0]["prompt_brief"]
    assert p == {"sessions_given": 3, "followed": 2, "share": 2 / 3}
    _, out, _ = world.ctx(capsys, "stats", "queries")
    assert "that then ran ctx show or ctx find: 2 of 3 (67%)" in out


def test_session_view_reports_the_prompt_brief(world, capsys):
    _prompted(world, "s-given", 150, ["sess-one", "sess-two"])
    _prompted(world, "s-none", 0)
    r = stats(world, capsys, "session", "s-given")[0]
    assert (r["prompt_brief_tokens"], r["prompt_brief_sessions"]) == (150, ["sess-one", "sess-two"])
    _, out, _ = world.ctx(capsys, "stats", "session", "s-given")
    assert "prompt brief: 150 tokens, named sess-one, sess-two" in out
    assert "prompt brief: none given" in world.ctx(capsys, "stats", "session", "s-none")[1]
    assert "prompt brief: not recorded" in world.ctx(capsys, "stats", "session", "sess-one")[1]


def test_imported_sessions_are_counted_apart(world, capsys):
    rec = SessionRecord(session_id="sess-imp", author="t", branch="main",
                        started_at=stamp(minutes_ago=60), ended_at=stamp(minutes_ago=50),
                        imported_at=stamp(), tool_calls={"read": 1}, ctx_calls={"find": 1},
                        turns=[TurnRecord(session_id="sess-imp", author="t", index=0,
                                          started_at=stamp(minutes_ago=60))])
    write_session(world.store, rec, Config())
    code, out, _ = world.ctx(capsys, "stats", "data")
    assert "  sessions: 2 (1 imported); turns: 3;" in out
    data = stats(world, capsys, "data")[0]
    assert data["sessions"] == 2 and data["imported_sessions"] == 1
    turns = stats(world, capsys, "turns")[0]
    assert turns["sessions"] == 1 and "sess-imp" not in json.dumps(turns)
    adoption = stats(world, capsys, "queries")[0]["adoption"]
    assert adoption["sessions"] == 1 and adoption["with_ctx"] == 0
    code, out, _ = world.ctx(capsys, "stats", "session", "sess-imp")
    assert "imported on " in out
