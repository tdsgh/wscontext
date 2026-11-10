import json
import os
import subprocess
import sys
import threading
import time

import pytest

from graph_support import config, store, table, unknown_language, write  # noqa: F401
from wscontext.capture import worker as capture_worker
from wscontext.graph import db, parse, worker
from wscontext.graph.build import claim_path, refresh
from wscontext.store.locks import FileLock

pytestmark = pytest.mark.usefixtures("unknown_language")

FILES = 40


def many(store, n: int = FILES, prefix: str = "m") -> None:
    for i in range(n):
        write(store, f"{prefix}{i:02d}.py", f"def f{i}():\n    return g{i}()\n")


def spawns() -> list:
    return capture_worker.spawn_worker.calls


def entries(store) -> list[dict]:
    path = store.logs / "hooks.jsonl"
    if not path.exists():
        return []
    return [e for e in map(json.loads, path.read_text().splitlines())
            if e["event"] == "worker:graph"]


def wait_for_entries(store, n: int, timeout: float = 60) -> list[dict]:
    deadline = time.monotonic() + timeout
    while len(found := entries(store)) < n:
        assert time.monotonic() < deadline, f"no worker:graph entry after {timeout}s: {found}"
        time.sleep(0.05)
    return found


def summary(store) -> dict:
    conn = db.connect(store, readonly=True)
    try:
        return db.summary(conn)
    finally:
        conn.close()


def test_a_budgeted_refresh_starts_the_worker_once_until_it_runs(store):
    many(store)

    first = refresh(store, config(), budget_ms=0)

    assert not first.complete and first.pending_files == FILES - 1
    assert spawns() == [(["refresh", str(store.root)], store.root, "wscontext.graph.worker")]
    assert claim_path(store).is_file()

    # the worker has not run yet: another incomplete refresh starts no second one
    assert not refresh(store, config(), budget_ms=0).complete
    assert len(spawns()) == 1

    assert worker.main(["refresh", str(store.root)]) == 0
    assert not claim_path(store).exists()
    [entry] = entries(store)
    assert entry["ok"] and entry["pending_files"] == 0 and entry["pending_history"] == 0
    assert table(store, "SELECT count(*) FROM files") == [(FILES,)]
    assert len(spawns()) == 1  # the worker's own refresh is unbudgeted and starts nothing

    # once that worker has run, the next incomplete refresh starts a new one
    many(store, prefix="n")
    assert not refresh(store, config(), budget_ms=0).complete
    assert len(spawns()) == 2


def test_a_stale_claim_does_not_stop_a_start(store):
    many(store)
    claim_path(store).parent.mkdir(parents=True, exist_ok=True)
    claim_path(store).write_text("1 0\n")
    old = time.time() - 120
    os.utime(claim_path(store), (old, old))

    refresh(store, config(), budget_ms=0)

    assert len(spawns()) == 1
    assert claim_path(store).stat().st_mtime > old


def test_busy_complete_and_unbudgeted_refreshes_start_no_worker(store):
    many(store, 3)
    with FileLock(db.lock_path(store), stale_after=600, timeout=1):
        assert refresh(store, config(), budget_ms=0).busy
    assert refresh(store, config(), budget_ms=60_000).complete
    many(store, 3, prefix="n")
    assert refresh(store, config()).complete

    assert spawns() == []
    assert not claim_path(store).exists()


def test_a_failed_start_releases_the_claim(store, monkeypatch):
    many(store)

    def broken(args, cwd, module="wscontext.capture.worker"):
        raise OSError("no interpreter")

    monkeypatch.setattr(capture_worker, "spawn_worker", broken)
    result = refresh(store, config(), budget_ms=0)

    assert not result.complete and result.parsed == 1
    assert not claim_path(store).exists()


def test_the_worker_completes_a_build_a_budgeted_query_left_incomplete(store, monkeypatch):
    monkeypatch.setattr(capture_worker, "spawn_worker", capture_worker.spawn_worker.real)
    many(store)

    partial = refresh(store, config(), budget_ms=0)
    assert not partial.complete and partial.pending_files == FILES - 1

    [entry] = wait_for_entries(store, 1)
    assert entry["ok"] and entry["parsed"] == FILES - 1, entry
    assert entry["pending_files"] == 0 and entry["parse_errors"] == 0
    assert isinstance(entry["ms"], int)
    assert table(store, "SELECT count(*) FROM symbols") == [(FILES,)]
    rest = refresh(store, config())
    assert rest.complete and rest.parsed == 0
    assert not claim_path(store).exists()


def test_a_build_finished_by_the_worker_counts_as_one_build(store):
    many(store)
    refresh(store, config(), budget_ms=0)
    assert summary(store)["last_build"] is None
    assert summary(store)["last_refresh"]["parsed"] == 1

    worker.main(["refresh", str(store.root)])

    s = summary(store)
    assert s["last_build"]["parsed"] == FILES and s["last_refresh"]["parsed"] == FILES - 1
    assert s["last_build"]["ms"] >= s["last_refresh"]["ms"]
    assert table(store, "SELECT key FROM meta WHERE key LIKE 'build_pending%'") == []


def test_two_workers_at_once_one_builds_one_skips(store, monkeypatch):
    many(store)
    real_facts = parse.facts
    holding, release = threading.Event(), threading.Event()

    def slow_facts(*args):
        holding.set()
        release.wait(30)
        return real_facts(*args)

    monkeypatch.setattr(parse, "facts", slow_facts)
    first = threading.Thread(target=worker.main, args=(["refresh", str(store.root)],))
    first.start()
    try:
        assert holding.wait(30)
        assert worker.main(["refresh", str(store.root)]) == 0
    finally:
        release.set()
        first.join(30)

    skipped, built = entries(store)
    assert skipped["ok"] and skipped["skipped"] == "another writer holds the graph lock"
    assert "parsed" not in skipped
    assert built["ok"] and built["parsed"] == FILES and "skipped" not in built
    assert table(store, "SELECT count(*) FROM files") == [(FILES,)]


def test_two_worker_processes_at_once_leave_one_complete_graph(store):
    many(store)
    command = [sys.executable, "-m", "wscontext.graph.worker", "refresh", str(store.root)]
    procs = [subprocess.Popen(command, cwd=store.root, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE) for _ in range(2)]
    outputs = [p.communicate(timeout=60) for p in procs]

    assert [p.returncode for p in procs] == [0, 0]
    assert outputs == [(b"", b""), (b"", b"")]
    found = entries(store)
    assert len(found) == 2 and all(e["ok"] for e in found), found
    assert all("skipped" in e or "parsed" in e for e in found)
    assert sum(e.get("parsed", 0) for e in found) == FILES
    assert refresh(store, config()).complete


def test_a_failure_is_logged_and_the_worker_exits_0(store):
    write(store, "a.py", "def f():\n    pass\n")
    db.db_path(store).mkdir(parents=True)  # no database can open here
    claim_path(store).write_text("1 0\n")
    command = [sys.executable, "-m", "wscontext.graph.worker"]

    broken = subprocess.run([*command, "refresh", str(store.root)], cwd=store.root,
                            capture_output=True, timeout=60)
    unknown = subprocess.run([*command, "explode", str(store.root)], cwd=store.root,
                             capture_output=True, timeout=60)

    assert (broken.returncode, broken.stdout, broken.stderr) == (0, b"", b"")
    assert unknown.returncode == 0
    first, second = entries(store)
    assert not first["ok"] and "OperationalError" in first["error"] and "ms" in first
    assert not second["ok"] and "unknown worker action" in second["error"]
    assert not claim_path(store).exists()


def test_no_language_part_logs_skipped(store):
    write(store, "a.py", "def f():\n    pass\n")
    write(store, ".wscontext/config.toml", "[graph]\nlanguages = [\"cobol\"]\n")

    assert worker.main(["refresh", str(store.root)]) == 0

    [entry] = entries(store)
    assert entry["ok"] and entry["skipped"] == "no language part available"
    assert entry["unavailable"] == ["cobol"]
    assert not db.db_path(store).exists()
