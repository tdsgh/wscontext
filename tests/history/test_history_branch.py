import logging
import subprocess
import threading

import pytest
from conftest import branch_blob, put_store_file, run_git

from wscontext.history import branch
from wscontext.history.branch import (HISTORY_REF, LEGACY_REF, commit_paths, merge_into_local,
                                      migrate_legacy, read_blob, read_tip, remove_paths,
                                      squash_unshared, union_merge)
from wscontext.store.paths import StorePaths

SESSION = "records/sessions/s-1.json"
RAW = "raw/sessions/2026-11-03-s-1.jsonl"


def _tree(repo, rev=HISTORY_REF) -> set[str]:
    return set(run_git(repo, "ls-tree", "-r", "--name-only", rev).split())


def _parents(repo, rev=HISTORY_REF) -> list[str]:
    return run_git(repo, "rev-list", "--parents", "-n", "1", rev).split()[1:]


def test_first_commit_is_an_orphan_with_exact_bytes(git_repo):
    store = StorePaths.resolve(git_repo)
    data = b'{"x": 1}\r\nsecond\n'
    paths = [put_store_file(store.local, SESSION, b"{}\n"), put_store_file(store.local, RAW, data)]
    tip = commit_paths(store, paths, "session s-1")
    assert tip == read_tip(store) == run_git(git_repo, "rev-parse", HISTORY_REF).strip()
    assert _parents(git_repo) == []
    assert _tree(git_repo) == {SESSION, RAW}
    assert branch_blob(git_repo, tip, RAW) == data
    assert run_git(git_repo, "log", "-1", "--format=%s", tip).strip() == "session s-1"


def test_next_commit_builds_on_the_tip(git_repo):
    store = StorePaths.resolve(git_repo)
    first = commit_paths(store, [put_store_file(store.local, SESSION, b"v1\n"),
                                 put_store_file(store.local, RAW, b"line\n")], "one")
    other = "records/checkpoints/" + "ab" * 20 + ".json"
    second = commit_paths(store, [put_store_file(store.local, RAW, b"line\nline\n"),
                                  put_store_file(store.local, other, b"{}\n")], "two")
    assert _parents(git_repo, second) == [first]
    assert _tree(git_repo) == {SESSION, RAW, other}
    assert branch_blob(git_repo, second, SESSION) == b"v1\n"
    assert branch_blob(git_repo, second, RAW) == b"line\nline\n"


def test_unchanged_content_makes_no_commit(git_repo):
    store = StorePaths.resolve(git_repo)
    path = put_store_file(store.local, SESSION, b"same\n")
    tip = commit_paths(store, [path], "one")
    assert commit_paths(store, [path], "again") == tip
    assert run_git(git_repo, "rev-list", "--count", HISTORY_REF).strip() == "1"


def test_concurrent_writers_all_land(git_repo):
    store = StorePaths.resolve(git_repo)
    writers, rounds = 4, 3
    barrier = threading.Barrier(writers)
    errors = []

    def write(n):
        try:
            barrier.wait()
            for r in range(rounds):
                p = put_store_file(store.local, f"records/sessions/w{n}-{r}.json", b"{}\n")
                commit_paths(store, [p], f"writer {n} round {r}")
        except Exception as e:  # surfaced below
            errors.append(e)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    expected = {f"records/sessions/w{n}-{r}.json" for n in range(writers) for r in range(rounds)}
    assert _tree(git_repo) == expected
    assert run_git(git_repo, "rev-list", "--count", HISTORY_REF).strip() == str(writers * rounds)
    assert run_git(git_repo, "rev-list", "--min-parents=2", HISTORY_REF) == ""


def test_working_tree_index_and_head_untouched(git_repo):
    store = StorePaths.resolve(git_repo)
    head = run_git(git_repo, "rev-parse", "HEAD")
    branch = run_git(git_repo, "symbolic-ref", "HEAD")
    index = (git_repo / ".git" / "index").read_bytes()
    commit_paths(store, [put_store_file(store.local, SESSION, b"{}\n"),
                         put_store_file(store.local, "raw/subagents/s-1/ag.jsonl", b"x\n")], "a")
    remove_paths(store, [store.local / "raw" / "subagents" / "s-1"])
    assert run_git(git_repo, "status", "--porcelain", "--ignored") == ""
    assert run_git(git_repo, "rev-parse", "HEAD") == head
    assert run_git(git_repo, "symbolic-ref", "HEAD") == branch
    assert (git_repo / ".git" / "index").read_bytes() == index
    assert run_git(git_repo, "ls-files") == "a.txt\n"


def test_remove_paths_drops_files_and_directories(git_repo):
    store = StorePaths.resolve(git_repo)
    keep = "records/sessions/s-2.json"
    paths = [put_store_file(store.local, rel, b"x\n")
             for rel in (SESSION, RAW, keep, "raw/subagents/s-1/ag-1.jsonl")]
    before = commit_paths(store, paths, "all")
    after = remove_paths(store, [store.local / "records" / "sessions" / "s-1.json",
                                 store.local / "raw" / "sessions" / "2026-11-03-s-1.jsonl",
                                 store.local / "raw" / "subagents" / "s-1"])
    assert _parents(git_repo, after) == [before]
    assert _tree(git_repo) == {keep}
    assert run_git(git_repo, "ls-tree", HISTORY_REF).split()[-1] == "records"


def test_remove_paths_without_a_branch_is_a_no_op(git_repo):
    store = StorePaths.resolve(git_repo)
    assert remove_paths(store, [store.local / "records" / "sessions" / "s-1.json"]) is None
    assert read_tip(store) is None


@pytest.mark.parametrize("rel", ["index/history.db", "state/active.json", "logs/hooks.jsonl",
                                 "records"])
def test_paths_outside_synced_dirs_are_rejected(git_repo, rel):
    store = StorePaths.resolve(git_repo)
    with pytest.raises(ValueError):
        commit_paths(store, [store.local.joinpath(*rel.split("/"))], "x")
    assert read_tip(store) is None


def test_path_outside_the_store_is_rejected(git_repo):
    store = StorePaths.resolve(git_repo)
    with pytest.raises(ValueError):
        commit_paths(store, [git_repo / "a.txt"], "x")


def _diverge(shared_remote, a_rel, a_data, b_rel, b_data):
    """Clone a publishes one commit to the bare remote; clone b commits its own and fetches."""
    bare, a, b = shared_remote
    sa, sb = StorePaths.resolve(a), StorePaths.resolve(b)
    a_tip = commit_paths(sa, [put_store_file(sa.local, a_rel, a_data)], "a")
    run_git(a, "push", "-q", "origin", f"{HISTORY_REF}:{HISTORY_REF}")
    b_tip = commit_paths(sb, [put_store_file(sb.local, b_rel, b_data)], "b")
    run_git(b, "fetch", "-q", "origin", f"+{HISTORY_REF}:refs/wscontext/remotes/origin/history")
    remote = run_git(b, "rev-parse", "refs/wscontext/remotes/origin/history").strip()
    assert remote == a_tip
    return sb, b, b_tip, a_tip


def test_union_merge_of_two_clones(shared_remote):
    sb, b, b_tip, a_tip = _diverge(shared_remote, "records/sessions/s-a.json", b"a\n",
                                   "records/sessions/s-b.json", b"b\n")
    merged = union_merge(sb, b_tip, a_tip)
    assert _parents(b, merged) == [b_tip, a_tip]
    assert _tree(b, merged) == {"records/sessions/s-a.json", "records/sessions/s-b.json"}
    assert read_tip(sb) == b_tip  # union_merge alone moves no ref
    # merge_into_local makes its own merge commit: same parents and tree, but the commit
    # timestamp (whole seconds) may differ from the one above, so the SHA may too.
    landed = merge_into_local(sb, a_tip)
    assert read_tip(sb) == landed
    assert _parents(b, landed) == [b_tip, a_tip]
    tree = run_git(b, "rev-parse", f"{merged}^{{tree}}")
    assert run_git(b, "rev-parse", f"{landed}^{{tree}}") == tree


def test_conflict_keeps_the_larger_blob_and_logs(shared_remote, caplog):
    sb, b, b_tip, a_tip = _diverge(shared_remote, SESSION, b"a much longer remote copy\n",
                                   SESSION, b"short\n")
    with caplog.at_level(logging.WARNING):
        merged = union_merge(sb, b_tip, a_tip)
    assert branch_blob(b, merged, SESSION) == b"a much longer remote copy\n"
    assert SESSION in caplog.text and "remote" in caplog.text


def test_merge_fast_forwards_when_one_side_contains_the_other(git_repo):
    store = StorePaths.resolve(git_repo)
    first = commit_paths(store, [put_store_file(store.local, SESSION, b"1\n")], "1")
    second = commit_paths(store, [put_store_file(store.local, RAW, b"2\n")], "2")
    assert union_merge(store, None, second) == second
    assert union_merge(store, first, second) == second
    assert union_merge(store, second, first) == second
    assert union_merge(store, second, second) == second


def _to_legacy(repo) -> str:
    """Move the local history to where stage 1 kept it; returns its tip."""
    tip = run_git(repo, "rev-parse", HISTORY_REF).strip()
    run_git(repo, "update-ref", LEGACY_REF, tip)
    run_git(repo, "update-ref", "-d", HISTORY_REF, tip)
    return tip


def _stage1_commit(store, rel: str) -> str:
    """What a stage-1 writer does: commit one file on top of `LEGACY_REF` by CAS."""
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=store.root, check=True,
                          input=rel.encode(), capture_output=True).stdout.decode().strip()
    while True:
        old = read_tip(store, LEGACY_REF)
        base = run_git(store.root, "rev-parse", f"{old}^{{tree}}").strip() if old else None
        tree = branch._write_tree(store, base, {rel: ("100644", blob)})
        new = branch._commit(store, tree, [old] if old else [], f"stage 1 {rel}")
        try:
            run_git(store.root, "update-ref", LEGACY_REF, new, old or "0" * 40)
            return new
        except subprocess.CalledProcessError:
            continue


def test_a_writer_moves_the_legacy_ref_first(git_repo):
    store = StorePaths.resolve(git_repo)
    legacy = commit_paths(store, [put_store_file(store.local, SESSION, b"old\n")], "stage 1")
    _to_legacy(git_repo)
    assert read_tip(store) is None
    tip = commit_paths(store, [put_store_file(store.local, RAW, b"new\n")], "stage 2")
    assert _parents(git_repo, tip) == [legacy]
    assert _tree(git_repo) == {SESSION, RAW}
    assert run_git(git_repo, "for-each-ref", LEGACY_REF) == ""
    assert run_git(git_repo, "branch", "--list") == "* main\n"


def test_migration_merges_a_diverged_legacy_ref(git_repo):
    store = StorePaths.resolve(git_repo)
    ours = commit_paths(store, [put_store_file(store.local, SESSION, b"ours\n")], "stage 2")
    theirs = _stage1_commit(store, "records/sessions/s-old.json")
    assert migrate_legacy(store) is True
    assert _parents(git_repo) == [ours, theirs]
    assert _tree(git_repo) == {SESSION, "records/sessions/s-old.json"}
    assert read_tip(store, LEGACY_REF) is None
    assert migrate_legacy(store) is False


@pytest.mark.parametrize("entry", ["merge_into_local", "squash_unshared"])
def test_every_writer_entry_point_migrates(git_repo, entry):
    store = StorePaths.resolve(git_repo)
    first = commit_paths(store, [put_store_file(store.local, SESSION, b"1\n")], "1")
    _stage1_commit(store, "records/sessions/s-old.json")
    if entry == "merge_into_local":
        merge_into_local(store, first)
    else:
        squash_unshared(store, [])
    assert read_tip(store, LEGACY_REF) is None
    assert _tree(git_repo) == {SESSION, "records/sessions/s-old.json"}


def test_migration_under_concurrent_writers(git_repo):
    """Stage-2 writers, explicit migrations and a stage-1 writer that keeps recreating the
    legacy ref all race; after a last migration every file is on the history ref."""
    store = StorePaths.resolve(git_repo)
    for n in range(3):
        _stage1_commit(store, f"records/sessions/old-{n}.json")
    writers, rounds = 4, 3
    barrier = threading.Barrier(writers + 2)
    errors = []

    def stage2(n):
        try:
            barrier.wait()
            for r in range(rounds):
                p = put_store_file(store.local, f"records/sessions/w{n}-{r}.json", b"{}\n")
                commit_paths(store, [p], f"writer {n} round {r}")
        except Exception as e:  # surfaced below
            errors.append(e)

    def stage1():
        try:
            barrier.wait()
            for r in range(rounds):
                _stage1_commit(store, f"records/sessions/late-{r}.json")
        except Exception as e:
            errors.append(e)

    def migrator():
        try:
            barrier.wait()
            for _ in range(rounds):
                migrate_legacy(store)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=stage2, args=(n,)) for n in range(writers)]
    threads += [threading.Thread(target=stage1), threading.Thread(target=migrator)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    migrate_legacy(store)
    expected = {f"records/sessions/w{n}-{r}.json" for n in range(writers) for r in range(rounds)}
    expected |= {f"records/sessions/old-{n}.json" for n in range(3)}
    expected |= {f"records/sessions/late-{r}.json" for r in range(rounds)}
    assert _tree(git_repo) == expected
    assert read_tip(store, LEGACY_REF) is None


def test_read_blob(git_repo):
    store = StorePaths.resolve(git_repo)
    assert read_blob(store, RAW) is None
    path = put_store_file(store.local, RAW, b"one\r\ntwo\n")
    commit_paths(store, [path], "raw")
    path.unlink()
    assert read_blob(store, RAW) == b"one\r\ntwo\n"
    assert read_blob(store, "raw/sessions/missing.jsonl") is None
    assert read_blob(store, "raw") is None  # a tree, not a file
