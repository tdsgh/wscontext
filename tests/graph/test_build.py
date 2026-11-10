import sqlite3
import threading

import pytest

from conftest import run_git
from graph_support import (ToyPart, config, register, snapshot, store, table, toy,  # noqa: F401
                           unknown_language, write)
from wscontext.graph import db
from wscontext.graph.build import MAX_FILE_BYTES, rebuild, refresh
from wscontext.graph.languages import load_parts
from wscontext.graph.languages.base import LanguageUnavailable
from wscontext.store.locks import FileLock

pytestmark = pytest.mark.usefixtures("unknown_language")


def names(store, path):
    return [r[0] for r in table(store, "SELECT qualname FROM symbols WHERE path=? ORDER BY id",
                                (path,))]


def test_refresh_builds_and_is_idempotent(store):
    write(store, "a.py", "def f():\n    return g()\n\n\ndef g():\n    pass\n")
    write(store, "b.py", "import a\n\n\ndef main():\n    a.f()\n")
    first = refresh(store, config())
    assert first.complete and not first.busy and first.parsed == 2
    assert first.pending_files == 0 and first.parse_errors == ()
    before = snapshot(store)
    assert names(store, "a.py") == ["f", "g"]
    assert table(store, "SELECT target FROM imports WHERE path='b.py'") == [("a.py",)]

    again = refresh(store, config())
    assert again.complete and again.parsed == 0
    assert snapshot(store) == before


def test_an_edited_file_reparses_alone(store):
    write(store, "a.py", "def f():\n    pass\n")
    write(store, "b.py", "def h():\n    pass\n")
    refresh(store, config())
    b_rows = table(store, "SELECT * FROM symbols WHERE path='b.py'")

    write(store, "a.py", "def k():\n    pass\n")  # same size, likely the same mtime tick
    result = refresh(store, config())

    assert result.parsed == 1
    assert names(store, "a.py") == ["k"]
    assert table(store, "SELECT * FROM symbols WHERE path='b.py'") == b_rows


def test_uncommitted_and_untracked_files_show_and_ignored_ones_do_not(store):
    write(store, "a.py", "def f():\n    pass\n")
    write(store, ".gitignore", "ignored.py\n")
    run_git(store.root, "add", "-A")
    run_git(store.root, "commit", "-q", "-m", "a")
    refresh(store, config())

    write(store, "a.py", "def f():\n    pass\n\n\ndef added():\n    pass\n")
    write(store, "new.py", "class New:\n    pass\n")
    write(store, "ignored.py", "def hidden():\n    pass\n")
    refresh(store, config())

    assert names(store, "a.py") == ["f", "added"]
    assert names(store, "new.py") == ["New"]
    assert table(store, "SELECT path FROM files ORDER BY path") == [("a.py",), ("new.py",)]


def test_a_deleted_file_loses_its_rows_and_its_importers_unresolve(store):
    write(store, "a.py", "import b\n\nb.run()\n")
    write(store, "b.py", "def run():\n    helper()\n")
    run_git(store.root, "add", "-A")
    run_git(store.root, "commit", "-q", "-m", "ab")  # b.py stays listed by Git after deletion
    refresh(store, config())
    assert table(store, "SELECT target FROM imports WHERE path='a.py'") == [("b.py",)]

    (store.root / "b.py").unlink()
    result = refresh(store, config())

    assert result.complete and result.parsed == 0
    for t in ("files", "symbols", "imports", "calls"):
        assert table(store, f"SELECT count(*) FROM {t} WHERE path='b.py'") == [(0,)]
    assert table(store, "SELECT target FROM imports WHERE path='a.py'") == [(None,)]


def test_an_added_file_resolves_an_import_without_reparsing_the_importer(store):
    write(store, "a.py", "from pkg import util\n")
    refresh(store, config())
    assert table(store, "SELECT target FROM imports WHERE path='a.py'") == [(None,)]

    write(store, "pkg/__init__.py", "")
    write(store, "pkg/util.py", "def u():\n    pass\n")
    result = refresh(store, config())

    assert result.parsed == 2
    assert table(store, "SELECT target FROM imports WHERE path='a.py'") == [("pkg/util.py",)]


def test_raising_a_part_version_reparses_only_its_language(store, toy):
    write(store, "a.py", "def f():\n    pass\n")
    write(store, "x.toy", "def x():\n    pass\n")
    write(store, "y.toy", "def y():\n    pass\n")
    assert refresh(store, config("python", "toy")).parsed == 3
    py_rows = table(store, "SELECT * FROM symbols WHERE path='a.py'")

    toy.version = 2
    result = refresh(store, config("python", "toy"))

    assert result.parsed == 2
    assert table(store, "SELECT path, lang_version FROM files ORDER BY path") == [
        ("a.py", 1), ("x.toy", 2), ("y.toy", 2)]
    assert table(store, "SELECT * FROM symbols WHERE path='a.py'") == py_rows


def test_switching_a_language_off_removes_only_its_rows(store, toy):
    write(store, "a.py", "def f():\n    x()\n")
    write(store, "x.toy", "def x():\n    pass\n")
    refresh(store, config("python", "toy"))
    py_before = {k: [r for r in v if "a.py" in r] for k, v in snapshot(store).items()}

    result = refresh(store, config("python"))

    assert result.parsed == 0 and "toy" in result.off and "python" not in result.off
    after = snapshot(store)
    assert {k: [r for r in v if "x.toy" in r] for k, v in after.items()} == {
        "files": [], "symbols": [], "imports": [], "calls": []}
    assert {k: [r for r in v if "a.py" in r] for k, v in after.items()} == py_before


def test_a_part_that_fails_to_load_is_unavailable_while_the_others_build(store, monkeypatch):
    class Broken(ToyPart):
        name = "broken"
        extensions = (".brk",)

        def load(self):
            raise LanguageUnavailable("no grammar")

    register(monkeypatch, "missing", None)
    register(monkeypatch, "broken", Broken())
    write(store, "a.py", "def f():\n    pass\n")
    write(store, "b.brk", "def b():\n    pass\n")

    result = refresh(store, config("python", "missing", "broken", "cobol"))

    assert result.complete
    assert result.unavailable == ("missing", "broken", "cobol")
    assert table(store, "SELECT path FROM files") == [("a.py",)]


def test_load_parts_never_raises(monkeypatch):
    register(monkeypatch, "missing", None)
    parts, unavailable = load_parts(["missing", "python", "python", "cobol"])
    assert [p.name for p in parts] == ["python"]
    assert unavailable == ["missing", "cobol"]


def test_the_budget_stops_a_large_build_and_the_next_refresh_finishes(store):
    for n in range(40):
        write(store, f"m{n:02d}.py", f"def f{n}():\n    return g{n}()\n")

    partial = refresh(store, config(), budget_ms=0)

    # at least one file per call; what was parsed is committed
    assert not partial.complete and not partial.busy
    assert partial.parsed == 1 and partial.pending_files == 39
    assert table(store, "SELECT count(*) FROM files") == [(1,)]

    rest = refresh(store, config())
    assert rest.complete and rest.parsed == 39 and rest.pending_files == 0
    assert table(store, "SELECT count(*) FROM symbols") == [(40,)]


def test_the_budget_counts_files_parsed_not_files_examined(store):
    write(store, "a.py", "def a():\n    return 1\n")
    write(store, "b.py", "def b():\n    return 1\n")
    refresh(store, config())
    # both were just written, so both are stored with mtime 0 and are examined again
    assert table(store, "SELECT mtime_ns FROM files") == [(0,), (0,)]
    write(store, "b.py", "def b():\n    return 2\n")

    result = refresh(store, config(), budget_ms=0)

    assert result.parsed == 1 and result.complete


def test_a_held_lock_answers_busy_and_readers_still_read(store):
    write(store, "a.py", "def f():\n    pass\n")
    refresh(store, config())
    write(store, "a.py", "def changed():\n    pass\n")

    with FileLock(db.lock_path(store), stale_after=600, timeout=1):
        result = refresh(store, config())
        assert result.busy and not result.complete and result.parsed == 0
        assert names(store, "a.py") == ["f"]

    assert refresh(store, config()).parsed == 1
    assert names(store, "a.py") == ["changed"]


def test_concurrent_refreshes_leave_one_complete_graph(store):
    for n in range(30):
        write(store, f"m{n:02d}.py", f"def f{n}():\n    pass\n")
    results, errors = [], []

    def go():
        try:
            results.append(refresh(store, config()))
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sum(r.parsed for r in results) == 30
    assert all(r.complete or r.busy for r in results)
    assert table(store, "SELECT count(*) FROM symbols") == [(30,)]


def test_exclude_patterns_leave_paths_out(store):
    write(store, "a.py", "def f():\n    pass\n")
    write(store, "vendor/lib.py", "def v():\n    pass\n")
    write(store, "src/vendor/lib.py", "def v():\n    pass\n")
    write(store, "src/gen/x.py", "def g():\n    pass\n")
    write(store, "src/api_pb2.py", "def p():\n    pass\n")
    write(store, "src/keep.py", "def k():\n    pass\n")
    refresh(store, config())
    assert len(table(store, "SELECT path FROM files")) == 6

    refresh(store, config(exclude=("vendor", "src/gen/", "*_pb2.py")))

    assert table(store, "SELECT path FROM files ORDER BY path") == [("a.py",), ("src/keep.py",)]


def test_a_file_over_the_size_limit_is_skipped(store):
    write(store, "big.py", "x = 1\n" * (MAX_FILE_BYTES // 6 + 1))
    write(store, "a.py", "def f():\n    pass\n")

    result = refresh(store, config())

    assert result.complete and result.parsed == 1
    assert table(store, "SELECT status FROM files WHERE path='big.py'") == [("skipped",)]
    assert table(store, "SELECT count(*) FROM symbols WHERE path='big.py'") == [(0,)]


def test_rebuild_gives_the_same_rows(store):
    write(store, "a.py", "import b\n\n\nclass A:\n    def m(self):\n        b.run()\n")
    write(store, "b.py", "def run():\n    pass\n")
    refresh(store, config())
    before = snapshot(store)

    result = rebuild(store, config())

    assert result.complete and result.parsed == 2
    assert snapshot(store) == before


def test_a_reader_needs_a_built_graph_and_a_writer_recreates_a_mismatched_one(store):
    with pytest.raises(db.GraphUnavailable):
        db.connect(store, readonly=True)
    write(store, "a.py", "def f():\n    pass\n")
    refresh(store, config())
    conn = db.connect(store)
    conn.execute("UPDATE meta SET value='0' WHERE key='schema_version'")
    conn.close()
    with pytest.raises(db.GraphUnavailable):
        db.connect(store, readonly=True)

    result = refresh(store, config())

    assert result.parsed == 1
    assert names(store, "a.py") == ["f"]


def test_a_reader_cannot_write(store):
    write(store, "a.py", "def f():\n    pass\n")
    refresh(store, config())
    conn = db.connect(store, readonly=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM files")
    finally:
        conn.close()


def test_summary_counts_by_language_and_status(store, toy):
    write(store, "a.py", "import os\n\n\ndef f():\n    g()\n")
    write(store, "bad.py", "def (:\n")
    write(store, "x.toy", "def x():\n    pass\n")
    refresh(store, config("python", "toy"))
    conn = db.connect(store, readonly=True)
    try:
        s = db.summary(conn)
    finally:
        conn.close()

    assert s["by_language"]["python"] == {"files": 2, "symbols": 1, "imports": 1, "calls": 1}
    assert s["by_language"]["toy"]["files"] == 1
    assert s["by_status"] == {"ok": 2, "error": 1, "skipped": 0}
    assert s["last_build"]["parsed"] == 3 and s["last_refresh"]["parsed"] == 3
    assert s["bytes"] > 0
