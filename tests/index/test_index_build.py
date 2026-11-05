import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

from conftest import run_git
from index_support import add_commit, add_session, store  # noqa: F401
from wscontext.history import branch as history
from wscontext.index import build
from wscontext.index.build import index_session, rebuild, refresh
from wscontext.index.db import SCHEMA_VERSION, IndexUnavailable, connect, db_path
from wscontext.index.search import brief_rows, search

import pytest

TURNS = [("how do we rotate the signing keys", "Use the rotate script.", "ls scripts"),
         ("add retry to the uploader", "Added exponential backoff.", "pytest -q")]


def rows(store, sql, *args):
    conn = connect(store, readonly=True)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def snapshot(store):
    return (rows(store, "SELECT kind, ref, turn, prompt, reply, tools, message FROM docs_fts "
                        "ORDER BY rowid"),
            rows(store, "SELECT * FROM sessions ORDER BY session_id"),
            rows(store, "SELECT * FROM commits ORDER BY sha"))


def test_refresh_indexes_exchanges_and_is_idempotent(store):
    add_session(store, "s1", TURNS, files=["src/up.py"])
    first = refresh(store)
    assert first["sessions_indexed"] == 1 and first["lag"] == 0
    before = snapshot(store)
    assert len(before[0]) == 2
    assert before[1][0][7] == "src/up.py" or "src/up.py" in before[1][0]
    again = refresh(store)
    assert again["sessions_indexed"] == 0
    assert snapshot(store) == before
    # tool calls are indexed, tool results are not
    conn = connect(store, readonly=True)
    assert [h.ref for h in search(conn, "pytest")] == ["s1"]
    conn.close()


def test_changed_copy_reindexes_and_title_is_first_prompt(store):
    path = add_session(store, "s1", TURNS[:1])
    refresh(store)
    assert rows(store, "SELECT title FROM sessions")[0][0] == "how do we rotate the signing keys"
    from index_support import transcript
    path.write_bytes(transcript(*TURNS))
    assert refresh(store)["sessions_indexed"] == 1
    assert len(rows(store, "SELECT 1 FROM docs_fts")) == 2


def test_record_change_updates_metadata_without_losing_exchanges(store):
    add_session(store, "s1", TURNS, ended=False)
    refresh(store)
    assert rows(store, "SELECT ended_at FROM sessions")[0][0] is None
    add_session(store, "s1", TURNS, ended=True, raw=False)
    refresh(store)
    assert rows(store, "SELECT ended_at FROM sessions")[0][0] == "2026-02-01T10:00:00Z"
    assert len(rows(store, "SELECT 1 FROM docs_fts")) == 2
    assert rows(store, "SELECT title FROM sessions")[0][0].startswith("how do we")


def test_forget_removes_rows_and_sources(store):
    add_session(store, "s1", TURNS)
    add_session(store, "s2", [("other topic entirely", "ok", "")])
    refresh(store)
    (store.records / "sessions" / "s1.json").unlink()
    res = refresh(store)
    assert res["sessions_removed"] == 1
    assert [r[0] for r in rows(store, "SELECT session_id FROM sessions")] == ["s2"]
    assert rows(store, "SELECT 1 FROM docs_fts WHERE ref='s1'") == []
    assert not [r for r in rows(store, "SELECT path FROM sources") if "s1" in r[0]]


def test_pruned_raw_keeps_rows(store):
    path = add_session(store, "s1", TURNS)
    refresh(store)
    before = snapshot(store)
    path.unlink()
    res = refresh(store)
    assert res["sessions_indexed"] == 0
    assert snapshot(store) == before


def test_pruned_raw_reads_branch_blob_when_never_indexed(store):
    path = add_session(store, "s1", TURNS)
    history.commit_paths(store, [path], "capture")
    path.unlink()
    refresh(store)
    conn = connect(store, readonly=True)
    assert [h.ref for h in search(conn, "backoff")] == ["s1"]
    conn.close()


def test_pruned_record_change_reindexes_from_branch(store):
    path = add_session(store, "s1", TURNS, ended=False)
    history.commit_paths(store, [path], "capture")
    refresh(store)
    path.unlink()
    add_session(store, "s1", TURNS, ended=True, raw=False)
    refresh(store)
    assert rows(store, "SELECT ended_at FROM sessions")[0][0] is not None
    assert len(rows(store, "SELECT 1 FROM docs_fts")) == 2


def test_subagent_exchanges_are_searchable(store):
    add_session(store, "s1", TURNS[:1], subagents={"a1": [("investigate zebra latency", "found it", "")]})
    refresh(store)
    conn = connect(store, readonly=True)
    hits = search(conn, "zebra")
    assert [(h.ref, h.turn) for h in hits] == [("s1", None)]
    conn.close()


def test_commits_indexed_pending_retried_and_linked(store):
    add_session(store, "s1", TURNS, files=["src/up.py"])
    sha = add_commit(store, "Fix uploader retry storm", ["s1"])
    missing = "f" * 40
    from wscontext.config import Config
    from wscontext.store.records import CheckpointRecord, write_checkpoint
    write_checkpoint(store, CheckpointRecord(sha=missing, author="bob", session_ids=["s1"]),
                     Config())
    res = refresh(store)
    assert res["commits_indexed"] == 1 and res["pending_commits"] == 1 and res["lag"] == 1
    conn = connect(store, readonly=True)
    hit = search(conn, "uploader retry storm", kind="commit")[0]
    assert (hit.ref, hit.branch, hit.author) == (sha, "main", "ann")
    assert conn.execute("SELECT commits FROM sessions").fetchone()[0] == sha
    conn.close()
    # the SHA arrives later (here: the record is replaced by a real commit) and is picked up
    (store.records / "checkpoints" / f"{missing}.json").unlink()
    res = refresh(store)
    assert res["pending_commits"] == 0 and res["commits_removed"] == 0


def test_index_session_handles_one_session(store):
    add_session(store, "s1", TURNS)
    add_session(store, "s2", TURNS[:1])
    res = index_session(store, "s1")
    assert res["sessions_indexed"] == 1
    assert [r[0] for r in rows(store, "SELECT session_id FROM sessions")] == ["s1"]
    with pytest.raises(ValueError):
        index_session(store, "../x")


def test_rebuild_matches_incremental(store):
    add_session(store, "s1", TURNS, files=["a.py"])
    add_commit(store, "Add the thing", ["s1"])
    refresh(store)
    before = snapshot(store)
    rebuild(store)
    assert snapshot(store) == before


def test_schema_mismatch_recreates_file(store):
    add_session(store, "s1", TURNS)
    refresh(store)
    conn = sqlite3.connect(db_path(store))
    conn.execute("UPDATE meta SET value='0'")
    conn.commit()
    conn.close()
    with pytest.raises(IndexUnavailable):
        connect(store, readonly=True)
    refresh(store)
    assert rows(store, "SELECT value FROM meta")[0][0] == str(SCHEMA_VERSION)
    assert len(rows(store, "SELECT 1 FROM docs_fts")) == 2


def test_readonly_without_index_raises(store):
    with pytest.raises(IndexUnavailable):
        connect(store, readonly=True)


def test_two_concurrent_refreshes_leave_one_copy(store):
    for i in range(6):
        add_session(store, f"s{i}", TURNS, files=[f"f{i}.py"])
    add_commit(store, "Concurrent commit", ["s0"])
    results, errors = [], []

    def go():
        try:
            results.append(refresh(store))
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert sum(r["sessions_indexed"] for r in results) == 6
    assert len(rows(store, "SELECT 1 FROM docs_fts WHERE kind='session'")) == 12
    assert len(rows(store, "SELECT 1 FROM docs_fts WHERE kind='commit'")) == 1
    assert len(rows(store, "SELECT 1 FROM sessions")) == 6


def test_two_processes_refresh_concurrently(store):
    for i in range(4):
        add_session(store, f"s{i}", TURNS)
    code = textwrap.dedent(f"""
        from pathlib import Path
        from wscontext.store.paths import StorePaths
        from wscontext.index.build import refresh
        print(refresh(StorePaths.resolve(Path({str(store.root)!r})))["sessions_indexed"])
    """)
    env = {**os.environ, "PYTHONPATH": str(Path(build.__file__).parents[2])}
    procs = [subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE,
                              text=True) for _ in range(2)]
    counts = [int(p.communicate()[0]) for p in procs]
    assert all(p.returncode == 0 for p in procs)
    assert sum(counts) == 4
    assert len(rows(store, "SELECT 1 FROM docs_fts")) == 8


def test_reader_queries_during_a_write(store):
    add_session(store, "s0", TURNS)
    refresh(store)
    reader = connect(store, readonly=True)
    writer = connect(store)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("DELETE FROM docs_fts")
    # the writer holds the write lock; the reader still sees the last committed state
    assert [h.ref for h in search(reader, "backoff")] == ["s0"]
    writer.execute("ROLLBACK")
    writer.close()
    reader.close()


def test_lock_held_reports_lag_unknown(store):
    from wscontext.store.locks import FileLock
    add_session(store, "s0", TURNS)
    with FileLock(store.index / "index.lock", stale_after=600):
        res = refresh(store, lock_timeout=0.1)
    assert res["locked"] and res["lag"] == -1


def test_brief_rows(store):
    add_session(store, "s1", TURNS, branch="feat", files=["a.py", "b.py"], date="2026-02-01")
    add_session(store, "s2", TURNS, branch="feat", date="2026-02-02")
    add_session(store, "s3", TURNS, branch="main", files=["a.py"], date="2026-02-03")
    add_session(store, "s4", TURNS, branch="main", files=["a.py", "b.py"], date="2026-02-04")
    add_session(store, "s5", TURNS, branch="main", files=["zzz.py"], date="2026-02-05")
    sha = add_commit(store, "Work on feat", ["s2"])
    refresh(store)
    conn = connect(store, readonly=True)
    out = brief_rows(conn, "feat", ["a.py", "b.py"], exclude=["s2"])
    assert [r["session_id"] for r in out["recent"]] == ["s1"]
    assert [r["session_id"] for r in out["files"]] == ["s4", "s3"]
    assert out["recent"][0]["title"].startswith("how do we")
    full = brief_rows(conn, "feat", [], exclude=[])
    assert [r["session_id"] for r in full["recent"]] == ["s2", "s1"]
    assert full["recent"][0]["commits"] == [sha]
    assert full["files"] == []
    assert brief_rows(conn, None, [], exclude="s1") == {"recent": [], "files": []}
    conn.close()
