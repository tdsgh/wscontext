import subprocess

from conftest import branch_blob, put_store_file, run_git, set_identity

from wscontext.config import Config, RedactionConfig, SyncConfig
from wscontext.history import sync
from wscontext.history.branch import HISTORY_REF, commit_paths, read_tip
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


def test_status_counts_commits_behind_after_a_plain_git_fetch(shared_remote):
    _, a, b = shared_remote
    sa, sb = StorePaths.resolve(a), StorePaths.resolve(b)
    _record(sa, "s-1")
    push(sa, ON)
    fetch(sb, ON)
    _record(sa, "s-2")
    _record(sa, "s-3")
    push(sa, ON)
    _record(sb, "s-4")
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=b, check=True)
    assert status(sb, ON) == SyncStatus("origin", 1, 2)
