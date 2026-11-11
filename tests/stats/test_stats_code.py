import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))

from cli_support import add_session, run_json, spawned, world  # noqa: F401

from wscontext.config import Config
from wscontext.graph import build, db
from wscontext.store.records import SessionRecord, write_session


def stats(world, capsys, *argv):
    return run_json(world, capsys, "stats", *argv)["results"]


def test_code_view_without_a_graph_says_so_and_still_lists_languages(world, capsys):
    d = stats(world, capsys, "code")[0]
    assert d["graph"] is None and d["changed_files"] is None
    assert d["languages"]["supported"] == ["javascript", "python", "typescript"]
    assert d["languages"]["enabled"] == ["python", "typescript", "javascript"]
    assert d["languages"]["unavailable"] == []
    _, out, _ = world.ctx(capsys, "stats", "code")
    assert "switched on: python, typescript, javascript" in out
    assert "no graph yet" in out and "files changed" not in out


def test_code_view_figures_text_and_json_parity(world, capsys):
    old = time.time() - 3600  # a file written in the last ~2 s may count as changed
    os.utime(world.repo / "src" / "app.py", (old, old))
    build.rebuild(world.store, Config())
    d = stats(world, capsys, "code")[0]
    py = d["graph"]["by_language"]["python"]
    assert py["files"] == 1 and py["symbols"] == 2
    assert d["graph"]["by_status"]["ok"] == 1 and d["graph"]["bytes"] > 0
    assert d["changed_files"] == 0
    assert d["graph"]["last_build"]["parsed"] == 1
    _, out, _ = world.ctx(capsys, "stats", "code")
    assert "python    1      2            0        0" in out
    assert "files changed since the graph last read them: 0" in out
    assert "files by status: ok 1, error 0, skipped 0" in out
    (world.repo / "src" / "new.py").write_text("def gamma():\n    pass\n")
    assert stats(world, capsys, "code")[0]["changed_files"] == 1
    _, out, _ = world.ctx(capsys, "stats", "code")
    assert "files changed since the graph last read them: 1" in out


def test_unknown_changed_files_is_not_shown_as_zero(world, capsys, monkeypatch):
    build.rebuild(world.store, Config())
    monkeypatch.setattr(build, "changed_files", lambda store, config: None)
    assert stats(world, capsys, "code")[0]["changed_files"] is None
    _, out, _ = world.ctx(capsys, "stats", "code")
    assert "files changed since the graph last read them: unknown" in out


def test_an_unavailable_language_is_listed(world, capsys, monkeypatch):
    from wscontext.graph import languages
    monkeypatch.setitem(languages.REGISTRY, "python", "wscontext.graph.languages.missing")
    d = stats(world, capsys, "code")[0]
    assert d["languages"]["unavailable"] == ["python"]
    assert "unavailable: python" in world.ctx(capsys, "stats", "code")[1]


def test_stats_code_never_refreshes_locks_or_starts_a_worker(world, capsys, monkeypatch):
    build.rebuild(world.store, Config())
    (world.repo / "src" / "new.py").write_text("def gamma():\n    pass\n")  # stale on purpose
    graph_file = db.db_path(world.store)
    before = (graph_file.read_bytes(), graph_file.stat().st_mtime_ns)
    lock = db.lock_path(world.store)
    lock.unlink(missing_ok=True)

    def boom(*a, **k):
        raise AssertionError("stats code must not write the graph")

    for name in ("refresh", "rebuild", "_run", "_spawn_worker"):
        monkeypatch.setattr(build, name, boom)
    real = db.connect
    monkeypatch.setattr(db, "connect", lambda store, readonly=False: real(store, readonly=True)
                        if readonly else boom())
    assert world.ctx(capsys, "stats", "code")[0] == 0
    assert world.ctx(capsys, "stats", "code", "--json")[0] == 0
    assert not lock.exists()
    assert (graph_file.read_bytes(), graph_file.stat().st_mtime_ns) == before


def _session(world, sid, calls):
    write_session(world.store, SessionRecord(session_id=sid, author="t@example.com",
                                             started_at="2026-01-08T10:00:00Z", ctx_calls=calls),
                  Config())


def test_adoption_of_the_graph_commands(world, capsys):
    _session(world, "s-code", {"code": 2})
    _session(world, "s-impact", {"impact": 1, "why": 3})
    _session(world, "s-name", {"why#name": 1, "why": 1})
    a = stats(world, capsys, "queries")[0]["adoption"]
    assert a["sessions"] == 4  # the fixture's own session counts
    assert (a["with_code"], a["with_impact"], a["with_why_name"]) == (1, 1, 1)
    assert a["with_code_share"] == 0.25
    _, out, _ = world.ctx(capsys, "stats", "queries")
    assert "sessions with ctx code: 1 (25%); with ctx impact: 1 (25%); " \
           "with ctx why on a name: 1 (25%)" in out


def _transcript(world, sid, command, started="2026-01-04T10:00:00Z"):
    add_session(world.store, sid, transcript=False, started=started)
    raw = world.store.raw / "sessions" / f"{started[:10]}-{sid}.jsonl"
    raw.write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "version": "2.0.5", "timestamp": started,
         "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "version": "2.0.5", "timestamp": started,
         "message": {"id": "m1", "role": "assistant", "content": [
             {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": command}}]}},
    ]) + "\n")


def test_share_of_sessions_that_opened_with_code_or_impact(world, capsys):
    _transcript(world, "s-a", "ctx code alpha", "2026-01-04T10:00:00Z")
    _transcript(world, "s-b", "ctx impact src/app.py", "2026-01-05T10:00:00Z")
    _transcript(world, "s-c", "ctx find alpha", "2026-01-06T10:00:00Z")
    q = stats(world, capsys, "queries")[0]
    # sess-one (fixture) opens with a Read: four sessions considered
    assert (q["sessions_considered"], q["ctx_first"], q["graph_first"]) == (4, 3, 2)
    assert q["graph_first_share"] == 0.5
    _, out, _ = world.ctx(capsys, "stats", "queries")
    assert "before any other search, read or ctx call: 2 of 4 sessions (50%)" in out
