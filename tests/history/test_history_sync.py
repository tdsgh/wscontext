import subprocess

from conftest import branch_blob, put_store_file, run_git, set_identity

from wscontext.config import Config, RedactionConfig, SyncConfig
from wscontext.history import sync
from wscontext.history.branch import HISTORY_REF, LEGACY_REF, commit_paths, read_tip
from wscontext.history.sync import SyncStatus, fetch, materialize, pending, push, status
from wscontext.store.paths import StorePaths

ON = Config(sync=SyncConfig(remote="origin", fetch_throttle_seconds=0))
THROTTLED = Config(sync=SyncConfig(remote="origin", fetch_throttle_seconds=3600))


def _record(store, sid: str, data: bytes = b"{}\n"):
    paths = [put_store_file(store.local, f"records/sessions/{sid}.json", data),
             put_store_file(store.local, f"raw/sessions/2026-11-03-{sid}.jsonl", data)]
    return commit_paths(store, paths, f"session {sid}")


def _remote_tip(bare) -> str | None:
    out = run_git(bare, "ls-remote", ".", HISTORY_REF).split()
    return out[0] if out else None


def _untouched(repo, head):
    assert run_git(repo, "status", "--porcelain", "--ignored") == ""
    assert run_git(repo, "rev-parse", "HEAD") == head


def test_nothing_is_shared_without_a_remote(shared_remote):
    bare, a, _ = shared_remote
    sa = StorePaths.resolve(a)
    _record(sa, "s-a")
    off = Config()
    assert push(sa, off) is False
    assert fetch(sa, off, force=True) is False
    assert pending(sa, off) == []
    assert status(sa, off) == SyncStatus(None, 1, 0)
    assert _remote_tip(bare) is None
    assert not (sa.state / "last_fetch").exists()


def test_push_publishes_and_tracks(shared_remote):
    bare, a, _ = shared_remote
    sa = StorePaths.resolve(a)
    head = run_git(a, "rev-parse", "HEAD")
    tip = _record(sa, "s-a")
    assert status(sa, ON) == SyncStatus("origin", 1, 0)
    assert push(sa, ON) is True
    assert _remote_tip(bare) == tip
    assert read_tip(sa, "refs/remotes/origin/wscontext/history") == tip
    assert status(sa, ON) == SyncStatus("origin", 0, 0)
    _untouched(a, head)


def test_pending_lists_sessions_not_yet_on_the_remote(shared_remote):
    _, a, _ = shared_remote
    sa = StorePaths.resolve(a)
    _record(sa, "s-a")
    assert pending(sa, ON) == ["s-a"]
    push(sa, ON)
    assert pending(sa, ON) == []
    _record(sa, "s-b")
    commit_paths(sa, [put_store_file(sa.local, "raw/sessions/2026-11-03-s-a.jsonl",
                                     b"{}\nmore\n")], "s-a grew")
    commit_paths(sa, [put_store_file(sa.local, "raw/subagents/s-c/ag.jsonl", b"x\n")], "s-c")
    assert pending(sa, ON) == ["s-a", "s-b", "s-c"]


def test_rejected_push_merges_and_retries(shared_remote):
    bare, a, b = shared_remote
    sa, sb = StorePaths.resolve(a), StorePaths.resolve(b)
    head = run_git(b, "rev-parse", "HEAD")
    a_tip = _record(sa, "s-a")
    assert push(sa, ON) is True
    b_tip = _record(sb, "s-b")
    assert push(sb, ON) is True
    merged = _remote_tip(bare)
    assert merged == read_tip(sb)
    assert run_git(b, "rev-list", "--parents", "-n", "1", merged).split()[1:] == [b_tip, a_tip]
    assert (sb.records / "sessions" / "s-a.json").exists()  # materialized by the merge
    _untouched(b, head)
    assert fetch(sa, ON) is True
    assert read_tip(sa) == merged
    assert (sa.raw / "sessions" / "2026-11-03-s-b.jsonl").read_bytes() == b"{}\n"


def test_push_skips_the_pre_push_hook(shared_remote, tmp_path):
    bare, a, _ = shared_remote
    sa = StorePaths.resolve(a)
    marker = tmp_path / "hook-ran"
    hook = a / ".git" / "hooks" / "pre-push"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    hook.chmod(0o755)
    tip = _record(sa, "s-a")
    assert push(sa, ON) is True
    assert _remote_tip(bare) == tip
    assert not marker.exists()


def test_push_never_raises(shared_remote, tmp_path):
    _, a, _ = shared_remote
    sa = StorePaths.resolve(a)
    _record(sa, "s-a")
    assert push(sa, Config(sync=SyncConfig(remote="nowhere"))) is False
    run_git(a, "remote", "add", "gone", str(tmp_path / "missing.git"))
    assert push(sa, Config(sync=SyncConfig(remote="gone"))) is False


def test_fetch_from_a_remote_without_history(shared_remote):
    _, a, _ = shared_remote
    sa = StorePaths.resolve(a)
    assert fetch(sa, ON) is False
    assert read_tip(sa) is None


def test_fetch_is_throttled(shared_remote):
    _, a, b = shared_remote
    sa, sb = StorePaths.resolve(a), StorePaths.resolve(b)
    _record(sa, "s-1")
    push(sa, ON)
    assert fetch(sb, THROTTLED) is True
    _record(sa, "s-2")
    push(sa, ON)
    assert fetch(sb, THROTTLED) is False
    assert not (sb.records / "sessions" / "s-2.json").exists()
    assert fetch(sb, THROTTLED, force=True) is True
    assert (sb.records / "sessions" / "s-2.json").exists()


def test_fresh_clone_recovers_history(shared_remote, tmp_path):
    bare, a, _ = shared_remote
    sa = StorePaths.resolve(a)
    transcript = b'{"type": "user"}\r\n{"type": "assistant"}\n'
    commit_paths(sa, [put_store_file(sa.local, "records/sessions/s-a.json", b"{}\n"),
                      put_store_file(sa.local, "raw/sessions/2026-11-03-s-a.jsonl", transcript),
                      put_store_file(sa.local, "raw/subagents/s-a/ag-1.jsonl", b"sub\n")], "s-a")
    push(sa, ON)
    c = tmp_path / "c"
    run_git(tmp_path, "clone", "-q", str(bare), str(c))
    set_identity(c)
    sc = StorePaths.resolve(c)
    head = run_git(c, "rev-parse", "HEAD")
    assert read_tip(sc) is None  # a clone does not create the local history branch
    assert fetch(sc, ON) is True
    assert read_tip(sc) == read_tip(sa)
    assert (sc.raw / "sessions" / "2026-11-03-s-a.jsonl").read_bytes() == transcript
    assert (sc.raw / "subagents" / "s-a" / "ag-1.jsonl").read_bytes() == b"sub\n"
    assert status(sc, ON) == SyncStatus("origin", 0, 0)
    assert pending(sc, ON) == []
    _untouched(c, head)


def _branch_only(store, rel: str, data: bytes, keep_local: bytes | None):
    """Put `data` on the branch at `rel`, then leave the local file as `keep_local` (None: absent)."""
    path = put_store_file(store.local, rel, data)
    tip = commit_paths(store, [path], rel)
    if keep_local is None:
        path.unlink()
    else:
        path.write_bytes(keep_local)
    return path, tip


def test_materialize_fills_missing_and_shorter_files_only(git_repo):
    store = StorePaths.resolve(git_repo)
    raw, tip = _branch_only(store, "raw/sessions/2026-11-03-s-1.jsonl", b"one\ntwo\n", b"one\n")
    rec, _ = _branch_only(store, "records/sessions/s-1.json", b"{}\n", None)
    assert materialize(store, Config()) == 2
    assert raw.read_bytes() == branch_blob(git_repo, tip, "raw/sessions/2026-11-03-s-1.jsonl")
    assert rec.read_bytes() == b"{}\n"
    # A newer, longer blob is written; a blob shorter than the local copy is not.
    _branch_only(store, "raw/sessions/2026-11-03-s-1.jsonl", b"one\ntwo\nthree\n", b"one\ntwo\n")
    _branch_only(store, "records/sessions/s-1.json", b'{"x":1}\n', b'{"grown": true}\n')
    assert materialize(store, Config()) == 1
    assert raw.read_bytes() == b"one\ntwo\nthree\n"
    assert rec.read_bytes() == b'{"grown": true}\n'
    # A blob already considered is restored when its local file goes missing.
    rec.unlink()
    assert materialize(store, Config()) == 1
    assert rec.read_bytes() == b'{"x":1}\n'


def test_materialize_redacts_with_local_rules(git_repo):
    store = StorePaths.resolve(git_repo)
    shared = b'{"text": "key AKIAABCDEFGHIJKLMNOP and acme-12345"}\n'
    raw, _ = _branch_only(store, "raw/sessions/2026-11-03-s-1.jsonl", shared, None)
    cfg = Config(redaction=RedactionConfig(extra_patterns=("acme-[0-9]+",)))
    assert materialize(store, cfg) == 1
    out = raw.read_bytes()
    assert b"AKIAABCDEFGHIJKLMNOP" not in out and b"acme-12345" not in out
    assert b"[REDACTED:aws-key]" in out and b"[REDACTED:custom-1]" in out


def test_redacted_copy_shorter_than_its_blob_is_not_rewritten(git_repo, monkeypatch):
    store = StorePaths.resolve(git_repo)
    shared = b'{"text": "AKIAABCDEFGHIJKLMNOP"}\n'
    raw, _ = _branch_only(store, "raw/sessions/2026-11-03-s-1.jsonl", shared, None)
    assert materialize(store, Config()) == 1
    first, stamp = raw.read_bytes(), raw.stat().st_mtime_ns
    assert len(first) < len(shared)

    def no_blob_reads(*args, **kwargs):
        raise AssertionError(f"blob read again: {args}")

    with monkeypatch.context() as m:
        m.setattr(sync, "git_bytes", no_blob_reads)
        assert materialize(store, Config()) == 0
    # Without the state file the blob is read once more; redaction is idempotent, so no write.
    (store.state / "materialized.json").unlink()
    assert materialize(store, Config()) == 0
    assert raw.read_bytes() == first and raw.stat().st_mtime_ns == stamp


def test_fetched_history_is_redacted_with_the_fetching_hosts_rules(shared_remote):
    _, a, b = shared_remote
    sa, sb = StorePaths.resolve(a), StorePaths.resolve(b)
    commit_paths(sa, [put_store_file(sa.local, "raw/sessions/2026-11-03-s-a.jsonl",
                                     b'{"text": "acme-12345"}\n')], "s-a")
    push(sa, ON)
    strict = Config(redaction=RedactionConfig(extra_patterns=("acme-[0-9]+",)),
                    sync=SyncConfig(remote="origin", fetch_throttle_seconds=0))
    assert fetch(sb, strict) is True
    copy = sb.raw / "sessions" / "2026-11-03-s-a.jsonl"
    assert copy.read_bytes() == b'{"text": "[REDACTED:custom-1]"}\n'
    stamp = copy.stat().st_mtime_ns
    commit_paths(sa, [put_store_file(sa.local, "records/sessions/s-a.json", b"{}\n")], "rec")
    push(sa, ON)
    assert fetch(sb, strict) is True
    assert copy.stat().st_mtime_ns == stamp
    assert (sb.records / "sessions" / "s-a.json").read_bytes() == b"{}\n"


def test_status_counts_commits_behind_after_a_git_fetch_of_the_history_ref(shared_remote):
    _, a, b = shared_remote
    sa, sb = StorePaths.resolve(a), StorePaths.resolve(b)
    _record(sa, "s-1")
    push(sa, ON)
    fetch(sb, ON)
    _record(sa, "s-2")
    _record(sa, "s-3")
    push(sa, ON)
    _record(sb, "s-4")
    subprocess.run(["git", "fetch", "-q", "origin",
                    f"+{HISTORY_REF}:refs/remotes/origin/wscontext/history"], cwd=b, check=True)
    assert status(sb, ON) == SyncStatus("origin", 1, 2)


def test_materialize_skips_pruned_paths(git_repo):
    store = StorePaths.resolve(git_repo)
    gone, _ = _branch_only(store, "raw/sessions/2026-11-03-s-1.jsonl", b"old\n", None)
    kept, _ = _branch_only(store, "raw/sessions/2026-11-03-s-2.jsonl", b"new\n", None)
    store.state.mkdir(parents=True, exist_ok=True)
    (store.state / "pruned.json").write_text('["raw/sessions/2026-11-03-s-1.jsonl"]\n')
    assert materialize(store, Config()) == 1
    assert not gone.exists() and kept.read_bytes() == b"new\n"
    # An object keyed by path reads the same; a later, longer blob stays pruned too.
    (store.state / "pruned.json").write_text('{"raw/sessions/2026-11-03-s-1.jsonl": "x"}\n')
    _branch_only(store, "raw/sessions/2026-11-03-s-1.jsonl", b"old\nolder\n", None)
    assert materialize(store, Config()) == 0
    assert not gone.exists()


LEGACY_TRACKING = "refs/remotes/origin/wscontext/legacy-history"


def _stage1_push(store, sid: str) -> str:
    """A stage-1 clone: commit a session and push it to the remote's legacy branch.

    The commit builds on any earlier legacy tip: `_record` moves it into `HISTORY_REF` first.
    """
    repo = store.root
    tip = _record(store, sid)
    run_git(repo, "update-ref", LEGACY_REF, tip)
    run_git(repo, "update-ref", "-d", HISTORY_REF)
    run_git(repo, "push", "-q", "--no-verify", "origin", f"{LEGACY_REF}:{LEGACY_REF}")
    return tip


def _refs(repo) -> str:
    return run_git(repo, "for-each-ref", "--format=%(refname)")


def test_clones_on_both_layouts_converge(shared_remote):
    bare, a, b = shared_remote
    sa, sb = StorePaths.resolve(a), StorePaths.resolve(b)
    old = _stage1_push(sa, "s-a")
    _record(sb, "s-b")
    assert fetch(sb, ON) is True
    assert (sb.records / "sessions" / "s-a.json").exists()
    assert read_tip(sb, LEGACY_TRACKING) == old
    assert push(sb, ON) is True
    assert _remote_tip(bare) == read_tip(sb)
    assert "refs/heads/wscontext/history" not in _refs(bare)
    assert read_tip(sb, LEGACY_TRACKING) is None
    # Clone a upgrades: its local legacy ref moves on its first fetch, then it converges.
    assert fetch(sa, ON) is True
    assert read_tip(sa, LEGACY_REF) is None
    assert read_tip(sa) == _remote_tip(bare)
    assert (sa.records / "sessions" / "s-b.json").exists()
    _record(sa, "s-c")
    assert push(sa, ON) is True
    assert fetch(sb, ON) is True
    assert read_tip(sb) == read_tip(sa) == _remote_tip(bare)


def test_a_recreated_legacy_branch_is_merged_and_removed_again(shared_remote):
    bare, a, b = shared_remote
    sa, sb = StorePaths.resolve(a), StorePaths.resolve(b)
    _record(sb, "s-b")
    push(sb, ON)
    _stage1_push(sa, "s-late")  # a stage-1 teammate pushes after the move
    assert "refs/heads/wscontext/history" in _refs(bare)
    assert fetch(sb, ON) is True and push(sb, ON) is True
    assert "refs/heads/wscontext/history" not in _refs(bare)
    assert run_git(bare, "cat-file", "-e", f"{HISTORY_REF}:records/sessions/s-late.json") == ""


def test_push_removes_the_legacy_branch_only_when_contained(shared_remote):
    bare, a, b = shared_remote
    sa, sb = StorePaths.resolve(a), StorePaths.resolve(b)
    old = _stage1_push(sa, "s-a")
    _record(sb, "s-b")
    # The legacy tip is known but not merged: the push leaves the branch alone.
    run_git(b, "fetch", "-q", "origin", f"+{LEGACY_REF}:{LEGACY_TRACKING}")
    assert push(sb, ON) is True
    assert _remote_tip(bare) == read_tip(sb)
    assert run_git(bare, "rev-parse", LEGACY_REF).strip() == old
    assert read_tip(sb, LEGACY_TRACKING) == old
    assert fetch(sb, ON) is True and push(sb, ON) is True
    assert "refs/heads/wscontext/history" not in _refs(bare)


def test_a_legacy_branch_moved_since_the_fetch_is_not_lost(shared_remote):
    bare, a, b = shared_remote
    sa, sb = StorePaths.resolve(a), StorePaths.resolve(b)
    _stage1_push(sa, "s-a")
    assert fetch(sb, ON) is True
    _stage1_push(sa, "s-a2")  # moves the remote legacy branch past b's lease
    _record(sb, "s-b")
    assert push(sb, ON) is True  # rejected once, fetched, merged, pushed
    assert "refs/heads/wscontext/history" not in _refs(bare)
    for sid in ("s-a", "s-a2", "s-b"):
        run_git(bare, "cat-file", "-e", f"{HISTORY_REF}:records/sessions/{sid}.json")


def test_a_legacy_branch_removed_by_a_teammate_does_not_block_a_push(shared_remote):
    bare, a, b = shared_remote
    sa, sb = StorePaths.resolve(a), StorePaths.resolve(b)
    _record(sa, "s-a")
    run_git(a, "update-ref", LEGACY_REF, read_tip(sa))
    run_git(a, "push", "-q", "--no-verify", "origin", f"{LEGACY_REF}:{LEGACY_REF}")
    run_git(a, "update-ref", "-d", LEGACY_REF)
    assert fetch(sb, ON) is True
    assert fetch(sa, ON) is False  # a already holds it all; the tracking ref is set
    assert read_tip(sa, LEGACY_TRACKING) is not None
    assert push(sb, ON) is True  # removes the remote legacy branch
    _record(sa, "s-a2")
    assert push(sa, ON) is True  # its lease is stale: rejected once, fetch prunes, pushed
    assert read_tip(sa, LEGACY_TRACKING) is None
    assert "refs/heads/wscontext/history" not in _refs(bare)
    assert _remote_tip(bare) == read_tip(sa)


def test_shared_tips_include_the_legacy_tracking_ref(shared_remote):
    _, a, _ = shared_remote
    sa = StorePaths.resolve(a)
    _stage1_push(sa, "s-a")
    sb = StorePaths.resolve(shared_remote[2])
    fetch(sb, ON)
    assert sync.shared_tips(sb) == [read_tip(sb, LEGACY_TRACKING)]
    assert "records/sessions/s-a.json" in sync.shared_paths(sb)


def test_a_plain_clone_or_fetch_has_no_history(shared_remote, tmp_path):
    bare, a, _ = shared_remote
    sa = StorePaths.resolve(a)
    _record(sa, "s-a")
    assert push(sa, ON) is True
    c = tmp_path / "c"
    run_git(tmp_path, "clone", "-q", str(bare), str(c))
    run_git(c, "fetch", "-q", "--all")
    assert "wscontext" not in _refs(c)
    assert "wscontext" not in run_git(c, "branch", "-a")
