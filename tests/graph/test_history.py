from types import SimpleNamespace

import pytest

from conftest import run_git
from graph_support import (config, snapshot, store, table, unknown_language, write)  # noqa: F401
from wscontext.config import Config, GraphConfig
from wscontext.graph import build, db, history, query
from wscontext.graph.build import refresh
from wscontext.store.records import (CheckpointRecord, SessionRecord, session_path,
                                     write_checkpoint, write_session)

pytestmark = pytest.mark.usefixtures("unknown_language")

CODE = "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n\n\ndef gamma():\n    return 3\n"


def commit(store, files: dict[str, str], message: str = "c") -> str:
    for rel, text in files.items():
        write(store, rel, text)
    run_git(store.root, "add", "-A")
    run_git(store.root, "commit", "-q", "-m", message)
    return run_git(store.root, "rev-parse", "HEAD").strip()


def link(store, sha: str, *session_ids: str, record: bool = True) -> None:
    cfg = Config()
    if record:
        for s in session_ids:
            write_session(store, SessionRecord(session_id=s, author="t"), cfg)
    write_checkpoint(store, CheckpointRecord(sha=sha, author="t", session_ids=list(session_ids)),
                     cfg)


def pairs(store):
    return table(store, "SELECT a, b, count FROM cochange ORDER BY a, b")


def touched(store):
    return table(store, "SELECT session_id, substr(sha, 1, 7), path, qualname FROM touched "
                        "ORDER BY session_id, sha, path, qualname")


def counted(store):
    return [p for (p,) in table(store, "SELECT path FROM cochange_commits ORDER BY path")
            if p.endswith(".py") or p.startswith("vendor")]


def conn_of(store):
    return db.connect(store, readonly=True)


def test_pairs_and_counts_on_a_scripted_history(store):
    commit(store, {"a.py": "x = 1\n", "b.py": "x = 1\n"})
    commit(store, {"a.py": "x = 2\n", "b.py": "x = 2\n", "c.py": "x = 2\n"})
    commit(store, {"a.py": "x = 3\n"})
    result = refresh(store, config())
    assert result.complete
    assert pairs(store) == [("a.py", "b.py", 2), ("a.py", "c.py", 1), ("b.py", "c.py", 1)]
    conn = conn_of(store)
    try:
        assert query.cochanged(conn, "a.py") == [query.CoChange("b.py", 2, 2 / 3)]
        assert query.cochanged(conn, "b.py") == [query.CoChange("a.py", 2, 1.0)]
        assert [c.path for c in query.cochanged(conn, "a.py", min_count=1)] == ["b.py", "c.py"]
        assert query.cochanged(conn, "missing.py") == []
    finally:
        conn.close()


def test_a_bulk_commit_counts_for_nothing(store):
    commit(store, {"a.py": "x = 1\n", "b.py": "x = 1\n"})
    commit(store, {f"bulk/f{i}.py": "x = 1\n" for i in range(31)} | {"a.py": "x = 2\n"})
    commit(store, {"a.py": "x = 3\n", "b.py": "x = 3\n"})
    refresh(store, config())
    assert pairs(store) == [("a.py", "b.py", 2)]
    assert table(store, "SELECT commits FROM cochange_commits WHERE path='a.py'") == [(2,)]


def test_the_window_is_the_last_n_commits(store):
    commit(store, {"a.py": "x = 1\n", "b.py": "x = 1\n"})
    commit(store, {"a.py": "x = 2\n", "c.py": "x = 2\n"})
    cfg = Config(graph=GraphConfig(languages=("python",), cochange_commits=1))
    refresh(store, cfg)
    assert pairs(store) == [("a.py", "c.py", 1)]


def test_new_commits_extend_and_a_rewound_head_recomputes(store):
    first = commit(store, {"a.py": "x = 1\n", "b.py": "x = 1\n"})
    refresh(store, config())
    second = commit(store, {"a.py": "x = 2\n", "b.py": "x = 2\n"})
    refresh(store, config())
    assert pairs(store) == [("a.py", "b.py", 2)]
    assert table(store, "SELECT value FROM meta WHERE key='cochange_head'") == [(second,)]

    run_git(store.root, "reset", "-q", "--hard", first)
    refresh(store, config())
    assert pairs(store) == [("a.py", "b.py", 1)]
    # an unchanged HEAD changes nothing
    before = snapshot(store), pairs(store)
    refresh(store, config())
    assert (snapshot(store), pairs(store)) == before


def test_a_session_links_the_functions_its_commit_changed(store):
    commit(store, {"m.py": CODE, "notes.txt": "n\n"})
    sha = commit(store, {"m.py": CODE.replace("return 2", "return 22"), "notes.txt": "n2\n"})
    link(store, sha, "s1")
    assert refresh(store, config()).complete
    assert touched(store) == [("s1", sha[:7], "m.py", ""), ("s1", sha[:7], "m.py", "beta"),
                              ("s1", sha[:7], "notes.txt", "")]
    conn = conn_of(store)
    try:
        links = query.sessions_for(conn, "m.py", "beta")
        assert [(x.session_id, x.sha, x.qualname) for x in links] == [("s1", sha, "beta")]
        assert query.sessions_for(conn, "m.py", "alpha") == []
        assert [x.session_id for x in query.sessions_for(conn, "m.py")] == ["s1"]
    finally:
        conn.close()


def test_a_change_lands_on_the_innermost_definition(store):
    code = "class K:\n    x = 1\n\n    def run(self):\n        return 1\n"
    commit(store, {"k.py": code})
    sha = commit(store, {"k.py": code.replace("return 1", "return 2")})
    sha2 = commit(store, {"k.py": code.replace("return 1", "return 2").replace("x = 1", "x = 9")})
    link(store, sha, "s1")
    link(store, sha2, "s2")
    refresh(store, config())
    assert [(r[0], r[3]) for r in touched(store) if r[3]] == [("s1", "K.run"), ("s2", "K")]


def test_a_function_moved_by_a_later_commit_keeps_its_session(store):
    commit(store, {"m.py": CODE})
    first = commit(store, {"m.py": CODE.replace("return 1", "return 11")})
    link(store, first, "s1")
    refresh(store, config())
    commit(store, {"m.py": "import os\n\n\n" + CODE.replace("return 1", "return 11")})
    refresh(store, config())
    conn = conn_of(store)
    try:
        assert [x.session_id for x in query.sessions_for(conn, "m.py", "alpha")] == ["s1"]
        assert conn.execute("SELECT start_line FROM symbols WHERE qualname='alpha'"
                            ).fetchone() == (4,)
    finally:
        conn.close()


def test_a_merge_commit_adds_nothing(store):
    commit(store, {"m.py": CODE})
    run_git(store.root, "checkout", "-q", "-b", "side")
    commit(store, {"side.py": "x = 1\n"})
    run_git(store.root, "checkout", "-q", "main")
    commit(store, {"m.py": CODE.replace("return 3", "return 33")})
    run_git(store.root, "merge", "-q", "--no-ff", "-m", "merge", "side")
    merge = run_git(store.root, "rev-parse", "HEAD").strip()
    link(store, merge, "s1")
    refresh(store, config())
    assert touched(store) == []
    assert table(store, "SELECT status FROM touched_done WHERE sha=?", (merge,)) == [("merge",)]


def test_a_forgotten_session_loses_its_rows(store):
    commit(store, {"m.py": CODE})
    sha = commit(store, {"m.py": CODE.replace("return 1", "return 11")})
    link(store, sha, "s1", "s2")
    refresh(store, config())
    assert {r[0] for r in touched(store)} == {"s1", "s2"}
    session_path(store, "s1").unlink()
    refresh(store, config())
    assert {r[0] for r in touched(store)} == {"s2"}
    # the record coming back links the commit again
    write_session(store, SessionRecord(session_id="s1", author="t"), Config())
    refresh(store, config())
    assert {r[0] for r in touched(store)} == {"s1", "s2"}


def test_a_missing_sha_is_retried_and_does_not_hold_the_graph_incomplete(store):
    commit(store, {"m.py": CODE})
    ghost = "f" * 40
    link(store, ghost, "s1")
    result = refresh(store, config())
    assert result.complete and result.pending_history == 0
    assert touched(store) == []
    assert table(store, "SELECT sha FROM touched_done") == []

    # the commit arrives: same SHA, found by a later refresh
    sha = commit(store, {"m.py": CODE.replace("return 1", "return 11")})
    link(store, sha, "s1")
    refresh(store, config())
    assert {r[1] for r in touched(store)} == {sha[:7]}


def test_a_spent_budget_leaves_commits_for_the_next_refresh(store, monkeypatch):
    commit(store, {"m.py": CODE})
    sha = commit(store, {"m.py": CODE.replace("return 1", "return 11")})
    link(store, sha, "s1")
    # the file walk sees the real clock; history sees a clock past any deadline
    monkeypatch.setattr(history, "time", SimpleNamespace(monotonic=lambda: 1e12))
    cut = refresh(store, config(), budget_ms=60_000)
    assert not cut.complete and cut.pending_history >= 1
    assert touched(store) == []
    monkeypatch.undo()
    assert refresh(store, config()).complete
    assert touched(store)


def test_rebuild_gives_the_same_rows(store):
    commit(store, {"m.py": CODE, "n.py": "x = 1\n"})
    sha = commit(store, {"m.py": CODE.replace("return 1", "return 11"), "n.py": "x = 2\n"})
    sha2 = commit(store, {"m.py": CODE.replace("return 3", "return 33")})
    link(store, sha, "s1")
    link(store, sha2, "s2")
    refresh(store, config())
    before = (pairs(store), touched(store), table(store, "SELECT * FROM cochange_commits"))
    assert before[0] and before[1]
    build.rebuild(store, config())
    assert (pairs(store), touched(store), table(store, "SELECT * FROM cochange_commits")
            ) == before


def test_an_excluded_path_adds_nothing_to_pairs_or_counts(store):
    commit(store, {"a.py": "x = 1\n", "b.py": "x = 1\n", "vendor/v.py": "x = 1\n"})
    commit(store, {"a.py": "x = 2\n", "vendor/v.py": "x = 2\n"})
    refresh(store, config(exclude=("vendor",)))
    assert pairs(store) == [("a.py", "b.py", 1)]
    assert counted(store) == ["a.py", "b.py"]
    conn = conn_of(store)
    try:
        assert query.cochanged(conn, "vendor/v.py", min_count=1) == []
        assert [c.path for c in query.cochanged(conn, "a.py", min_count=1)] == ["b.py"]
    finally:
        conn.close()


def test_the_bulk_cut_counts_only_paths_that_are_kept(store):
    commit(store, {"a.py": "x = 1\n", "b.py": "x = 1\n"}
           | {f"vendor/f{i}.py": "x = 1\n" for i in range(40)})
    refresh(store, config(exclude=("vendor",)))
    assert pairs(store) == [("a.py", "b.py", 1)]


def test_changing_exclude_recounts_without_a_new_commit(store):
    commit(store, {"a.py": "x = 1\n", "b.py": "x = 1\n", "c.py": "x = 1\n"})
    refresh(store, config())
    assert len(pairs(store)) == 3
    refresh(store, config(exclude=("c.py",)))
    assert pairs(store) == [("a.py", "b.py", 1)]
    assert counted(store) == ["a.py", "b.py"]
    refresh(store, config())
    assert len(pairs(store)) == 3
