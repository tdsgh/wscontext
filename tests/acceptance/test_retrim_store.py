"""The one-off store re-trim: stored copies under the current reduction list, unpushed history squashed."""

import json
import sys
from pathlib import Path

import pytest

from conftest import put_store_file, run_git

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "acceptance"))
import retrim_store as rs  # noqa: E402

from wscontext.adapters import claude_code  # noqa: E402
from wscontext.adapters.claude_code import ClaudeCodeAdapter, reduce  # noqa: E402
from wscontext.capture.worker import stored_sizes  # noqa: E402
from wscontext.config import Config  # noqa: E402
from wscontext.history.branch import HISTORY_REF, commit_paths, ls_blobs, read_tip  # noqa: E402
from wscontext.store.paths import StorePaths  # noqa: E402
from wscontext.store.records import (CheckpointRecord, SessionRecord, read_session,  # noqa: E402
                                     write_checkpoint, write_session)

SID = "sess-1"
CTX = {"sessionId": SID, "cwd": "/w", "version": "2.1.0", "gitBranch": "main",
       "entrypoint": "cli", "userType": "external"}
OWNER = {"type": "session-owner", "accountUuid": "acc-0001", "organizationUuid": "org-0001",
         "sessionId": SID}
PROMPT = "please note accountUuid acc-0001 in the docs"
RESULT = '{"accountUuid": "acc-0001"}'


def _line(obj: dict) -> bytes:
    return json.dumps(obj).encode() + b"\n"


def source(sid: str = SID) -> bytes:
    """A transcript whose first line with the keep-once fields is a hook_success attachment, with
    account and cost state between the turns, and the identifier mentioned in a prompt and a result."""
    ctx = {**CTX, "sessionId": sid}
    return b"".join(_line(o) for o in [
        {"type": "attachment", "attachment": {"type": "hook_success", "hookName": "SessionStart"},
         "uuid": "u0", "timestamp": "2026-11-06T10:00:00Z", **ctx},
        {**OWNER, "sessionId": sid},
        {"type": "user", "message": {"role": "user", "content": PROMPT}, "uuid": "u1",
         "timestamp": "2026-11-06T10:00:01Z", **ctx},
        {"type": "assistant", "uuid": "u2", "timestamp": "2026-11-06T10:00:02Z", **ctx,
         "message": {"role": "assistant", "id": "m1", "usage": {"input_tokens": 3, "output_tokens": 2},
                     "content": [{"type": "text", "text": "looking"},
                                 {"type": "tool_use", "id": "t1", "name": "Read", "input": {"p": "x"}}]}},
        {"type": "cost-state", "totalCostUSD": 0.1, "sessionId": sid},
        {"type": "user", "uuid": "u3", "timestamp": "2026-11-06T10:00:03Z", **ctx,
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                                   "content": RESULT}]}},
        {**OWNER, "sessionId": sid},
        {"type": "user", "message": {"role": "user", "content": "thanks"}, "uuid": "u4",
         "timestamp": "2026-11-06T10:01:00Z", **ctx},
    ])


def captured_before_wp10(data: bytes, monkeypatch) -> bytes:
    """The copy a capture stored while session-owner, cost-state and hook_success were not bookkeeping."""
    with monkeypatch.context() as m:
        m.setattr(claude_code, "BOOKKEEPING_TYPES",
                  claude_code.BOOKKEEPING_TYPES - {"session-owner", "cost-state"})
        m.setattr(claude_code, "BOOKKEEPING_ATTACHMENTS",
                  claude_code.BOOKKEEPING_ATTACHMENTS - {"hook_success"})
        return reduce(data)


def add_session(store: StorePaths, sid: str, copy: bytes) -> list[Path]:
    raw = put_store_file(store.local, f"raw/sessions/2026-11-06-{sid}.jsonl", copy)
    sizes = stored_sizes(raw, store.raw / "subagents" / sid)
    rec = write_session(store, SessionRecord(session_id=sid, author="t@example.com",
                                             started_at="2026-11-06T10:00:00Z",
                                             transcript_bytes={**sizes, "source": 999}), Config())
    return [raw, rec]


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A home directory whose `.claude.json` (Claude Code's account file) a test writes itself."""
    from wscontext.redact import account
    monkeypatch.setattr(account, "account_file_path", lambda: tmp_path / "home" / ".claude.json")
    return tmp_path / "home"


@pytest.fixture
def world(git_repo, monkeypatch):
    """One session captured before WP10, committed twice to the history, plus a checkpoint."""
    store = StorePaths.resolve(git_repo)
    copy = captured_before_wp10(source(), monkeypatch)
    head = copy.splitlines(keepends=True)
    paths = add_session(store, SID, b"".join(head[:4]))
    commit_paths(store, paths, f"wscontext: session {SID}")
    paths = add_session(store, SID, copy)
    sha = run_git(git_repo, "rev-parse", "HEAD").strip()
    point = write_checkpoint(store, CheckpointRecord(sha=sha, author="t@example.com",
                                                     session_ids=[SID]), Config())
    commit_paths(store, [*paths, point], f"wscontext: session {SID}")
    return git_repo, store, copy


def files(store: StorePaths) -> dict[str, bytes]:
    return {p.relative_to(store.local).as_posix(): p.read_bytes()
            for p in sorted(store.local.rglob("*")) if p.is_file()}


def reachable(repo: Path) -> dict[str, bytes]:
    out = {}
    for line in run_git(repo, "rev-list", "--objects", HISTORY_REF).splitlines():
        sha = line.split()[0]
        if run_git(repo, "cat-file", "-t", sha).strip() == "blob":
            out[sha] = run_git(repo, "cat-file", "blob", sha).encode()
    return out


def apply(repo: Path, tmp_path: Path, capsys) -> tuple[int, str]:
    code = rs.main(["--repo", str(repo), "--apply", "--backup", str(tmp_path / "backup")])
    return code, capsys.readouterr().out


def test_plain_reduce_of_a_stored_copy_loses_the_format_and_the_check_says_so(world):
    _, _, copy = world
    lossy = reduce(copy)
    assert rs.check_retrim(copy, lossy, 0, False)  # version only sat on the dropped line
    trimmed, dropped, carried = rs.rereduce(copy)
    assert rs.check_retrim(copy, trimmed, 0, False) == []
    assert dropped == {"session-owner": 2, "cost-state": 1, "attachment:hook_success": 1}
    assert carried == 1


def test_dry_run_writes_nothing(world, capsys):
    repo, store, _ = world
    before, tip = files(store), read_tip(store)
    assert rs.main(["--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "drops 1 attachment:hook_success, 1 cost-state, 2 session-owner" in out
    assert "these 2 commit(s) are replaced by one root commit" in out
    assert "dry run: nothing written" in out
    assert files(store) == before and read_tip(store) == tip


def test_apply_drops_only_the_named_kinds_and_keeps_message_text(world, tmp_path, capsys):
    repo, store, copy = world
    code, out = apply(repo, tmp_path, capsys)
    assert code == 0, out
    assert "verify: ok" in out
    new = (store.raw / "sessions" / f"2026-11-06-{SID}.jsonl").read_bytes()
    old_lines, new_lines = copy.splitlines(), new.splitlines()
    kept = [x for x in old_lines if json.loads(x)["type"] not in ("session-owner", "cost-state", "attachment")]
    assert [json.loads(x)["type"] for x in new_lines] == ["user", "assistant", "user", "user"]
    # the first kept line takes the keep-once fields only the dropped line carried; the rest keep their bytes
    assert json.loads(new_lines[0]) == {**json.loads(kept[0]), **CTX}
    assert new_lines[1:] == kept[1:]
    assert PROMPT in new.decode() and json.dumps(RESULT) in new.decode()
    assert rs.account_lines(new) == {}
    assert ClaudeCodeAdapter().parse(store.raw / "sessions" / f"2026-11-06-{SID}.jsonl").supported


def test_text_that_mentions_the_keys_is_not_a_change(git_repo, capsys):
    store = StorePaths.resolve(git_repo)
    clean = reduce(source())
    assert b"acc-0001" in clean and rs.account_lines(clean) == {}
    commit_paths(store, add_session(store, SID, clean), "wscontext: session")
    assert rs.main(["--repo", str(git_repo)]) == 0
    out = capsys.readouterr().out
    assert "copies to re-trim: 0" in out and "history rewrite: none needed" in out


def test_history_is_squashed_and_no_old_blob_is_reachable(world, tmp_path, capsys):
    repo, store, copy = world
    old = reachable(repo)
    assert any(b'"session-owner"' in b for b in old.values())
    code, out = apply(repo, tmp_path, capsys)
    assert code == 0, out
    assert run_git(repo, "rev-list", "--count", HISTORY_REF).strip() == "1"
    now = reachable(repo)
    assert not set(old) & {s for s, b in now.items() if b'"session-owner"' in b or b'"cost-state"' in b}
    assert not [s for s, b in now.items() if rs.account_lines(b)]
    tip = ls_blobs(store, read_tip(store))
    assert {"records/sessions/sess-1.json", f"raw/sessions/2026-11-06-{SID}.jsonl"} <= set(tip)
    assert [p for p in tip if p.startswith("records/checkpoints/")]
    # the backup bundle holds the replaced tip
    heads = run_git(repo, "bundle", "list-heads", str(tmp_path / "backup" / "history.bundle"))
    assert HISTORY_REF in heads
    assert (tmp_path / "backup" / "store" / "raw" / "sessions" / f"2026-11-06-{SID}.jsonl").read_bytes() == copy


def test_a_history_commit_landing_during_apply_survives_the_squash(world, tmp_path, capsys, monkeypatch):
    repo, store, _ = world
    real = rs.commit_paths

    def and_another_writer(store, paths, message):
        tip = real(store, paths, message)
        other = add_session(store, "sess-2", reduce(source("sess-2")))
        commit_paths(store, other, "wscontext: session sess-2")  # moves the tip the squash read
        return tip

    monkeypatch.setattr(rs, "commit_paths", and_another_writer)
    code, out = apply(repo, tmp_path, capsys)
    assert code == 0, out
    tip = ls_blobs(store, read_tip(store))
    assert "records/sessions/sess-2.json" in tip and "raw/sessions/2026-11-06-sess-2.jsonl" in tip
    assert run_git(repo, "rev-list", "--count", HISTORY_REF).strip() == "1"


def test_records_sizes_follow_the_copies(world, tmp_path, capsys):
    repo, store, _ = world
    code, out = apply(repo, tmp_path, capsys)
    assert code == 0, out
    rec = read_session(store, SID)
    raw = store.raw / "sessions" / f"2026-11-06-{SID}.jsonl"
    assert rec.transcript_bytes == {**stored_sizes(raw, store.raw / "subagents" / SID), "source": 999}
    assert rec.transcript_bytes["stored"] == raw.stat().st_size
    on_tip = run_git(repo, "cat-file", "blob", f"{HISTORY_REF}:records/sessions/{SID}.json")
    assert json.loads(on_tip)["transcript_bytes"] == rec.transcript_bytes


def test_checkpoints_and_session_resolution_stay(world, tmp_path, capsys):
    repo, store, _ = world
    point = next((store.records / "checkpoints").glob("*.json")).read_bytes()
    code, out = apply(repo, tmp_path, capsys)
    assert code == 0, out
    assert next((store.records / "checkpoints").glob("*.json")).read_bytes() == point
    assert "index: rebuilt, 1 sessions" in out


def test_refuses_with_a_remote(shared_remote, tmp_path, capsys, monkeypatch):
    _, clone, _ = shared_remote
    store = StorePaths.resolve(clone)
    commit_paths(store, add_session(store, SID, captured_before_wp10(source(), monkeypatch)), "s")
    before, tip = files(store), read_tip(store)
    assert rs.main(["--repo", str(clone)]) == 1
    assert "Git remote 'origin' is configured" in capsys.readouterr().out
    code, out = apply(clone, tmp_path, capsys)
    assert code == 1 and "apply refuses" in out
    assert files(store) == before and read_tip(store) == tip
    assert not (tmp_path / "backup").exists()


def test_refuses_with_a_tracking_ref(world, tmp_path, capsys):
    repo, store, _ = world
    run_git(repo, "update-ref", "refs/wscontext/remotes/origin/history", read_tip(store))
    before, tip = files(store), read_tip(store)
    code, out = apply(repo, tmp_path, capsys)
    assert code == 1 and "ref refs/wscontext/remotes/origin/history exists" in out
    assert files(store) == before and read_tip(store) == tip


def test_an_active_session_that_needs_a_change_blocks(world, tmp_path, capsys):
    repo, store, _ = world
    put_store_file(store.local, "state/active.json",
                   json.dumps({str(repo): {SID: "2026-11-06T10:00:00+00:00"}}).encode())
    before, tip = files(store), read_tip(store)
    code, out = apply(repo, tmp_path, capsys)
    assert code == 1
    assert f"session {SID} is active" in out and "finalize it with the sweep first" in out
    assert "wscontext.capture.worker sweep" in out
    assert files(store) == before and read_tip(store) == tip


def test_an_active_session_with_a_clean_copy_is_left_alone(world, tmp_path, capsys):
    repo, store, _ = world
    live = add_session(store, "live", reduce(source("live")))
    put_store_file(store.local, "state/active.json",
                   json.dumps({str(repo): {"live": "2099-01-01T00:00:00+00:00"}}).encode())
    kept = {p: p.read_bytes() for p in live}
    code, out = apply(repo, tmp_path, capsys)
    assert code == 0, out
    assert {p: p.read_bytes() for p in live} == kept
    assert "raw/sessions/2026-11-06-live.jsonl" not in ls_blobs(store, read_tip(store))


def test_a_held_worker_lock_stops_apply(world, tmp_path, capsys, monkeypatch):
    repo, store, _ = world
    monkeypatch.setattr(rs, "LOCK_TIMEOUT", 0.1)
    put_store_file(store.local, f"state/locks/worker-{SID}.lock", b"1 0\n")
    before, tip = files(store), read_tip(store)
    code = rs.main(["--repo", str(repo), "--apply", "--backup", str(tmp_path / "backup")])
    assert code == 1 and f"worker-{SID}.lock" in capsys.readouterr().err
    assert files(store) == before and read_tip(store) == tip


def test_apply_twice_has_nothing_to_do(world, tmp_path, capsys):
    repo, store, _ = world
    assert apply(repo, tmp_path, capsys)[0] == 0
    tip = read_tip(store)
    code = rs.main(["--repo", str(repo), "--apply", "--backup", str(tmp_path / "again")])
    assert code == 0 and "nothing to do" in capsys.readouterr().out
    assert read_tip(store) == tip and not (tmp_path / "again").exists()


def test_backup_must_be_outside_the_repository(world, capsys):
    repo, store, _ = world
    before = files(store)
    assert rs.main(["--repo", str(repo), "--apply", "--backup", str(repo / "bk")]) == 2
    assert rs.main(["--repo", str(repo), "--apply", "--backup", str(store.local / "bk")]) == 2
    assert files(store) == before and not (repo / "bk").exists()


def test_snapshots_are_reported_and_never_rewritten(world, tmp_path, capsys):
    repo, store, _ = world
    org = {"type": "attachment", "attachment": {"type": "credential_org", "organizationUuid": "org-0001"},
           "uuid": "c1", **CTX}
    snap = put_store_file(store.local, f"raw/sessions/2026-11-06-pc20261106T100000Z-{SID}.jsonl",
                          _line(org) + source())
    data = snap.read_bytes()
    code, out = apply(repo, tmp_path, capsys)
    assert code == 0, out
    assert "1 attachment:credential_org, 2 session-owner; local only" in out
    assert snap.read_bytes() == data


ACCOUNT = "3f2a9c1e-5b7d-4e8a-9c01-aa11bb22cc33"
ORG = "7d6e5f40-1a2b-4c3d-8e9f-0123456789ab"
OTHER = "11111111-2222-4333-8444-555555555555"


def with_account_ids(world, home, tmp_path):
    """The `world` store, its copy, a record and a log holding the made-up account IDs."""
    repo, store, _ = world
    home.mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"accountUuid": ACCOUNT, "organizationUuid": ORG}}))
    raw = next((store.raw / "sessions").glob("*.jsonl"))
    raw.write_bytes(raw.read_bytes() + _line(
        {"type": "user", "uuid": "u9", "message": {"role": "user", "content":
         f"mine {ACCOUNT} org {ORG.upper()} session {OTHER}"}, **CTX}))
    rec = store.records / "sessions" / f"{SID}.json"
    data = json.loads(rec.read_text())
    data["git_branch"] = f"user/{ACCOUNT}"
    rec.write_text(json.dumps(data, sort_keys=True))
    log = put_store_file(store.local, "logs/hook.log", f"ran for {ORG}\nnext {OTHER}\n".encode())
    commit_paths(store, [raw, rec], "wscontext: session")
    return repo, store, log


def test_dry_run_reports_account_ids_by_count_and_writes_nothing(world, home, tmp_path, capsys):
    repo, store, log = with_account_ids(world, home, tmp_path)
    before = files(store)
    assert rs.main(["--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "2 account-id replacement(s)" in out and f"logs/hook.log: 1 account-id" in out
    assert "records/sessions" in out
    assert not any(i in out for i in (ACCOUNT, ORG, ORG.upper())) and files(store) == before
    assert not (store.state / "account_ids.json").exists()


def test_apply_masks_copies_records_logs_and_the_squashed_history(world, home, tmp_path, capsys):
    repo, store, log = with_account_ids(world, home, tmp_path)
    code, out = apply(repo, tmp_path, capsys)
    assert code == 0 and "verify: ok" in out, out
    assert not any(i in out for i in (ACCOUNT, ORG, ORG.upper()))
    blobs = [*files(store).values(), *reachable(repo).values()]
    assert [] == [(n, b[max(0, b.lower().find(i.encode()) - 30):][:100]) for n, b in {**files(store), **reachable(repo)}.items()
                  for i in (ACCOUNT, ORG) if i.encode() in b.lower()]
    assert any(b"[REDACTED:account-id]" in b for b in files(store).values())
    assert log.read_bytes() == b"ran for [REDACTED:account-id]\nnext " + OTHER.encode() + b"\n"
    assert OTHER.encode() in next((store.raw / "sessions").glob("*.jsonl")).read_bytes()
    assert (store.state / "account_ids.json").is_file()
    assert not any(i.encode() in (store.state / "account_ids.json").read_bytes() for i in (ACCOUNT, ORG))


def test_a_snapshot_holding_an_id_is_listed_and_apply_does_not_pass(world, home, tmp_path, capsys):
    repo, store, _ = with_account_ids(world, home, tmp_path)
    put_store_file(store.local, f"raw/sessions/2026-11-06-pc20261106T100000Z-{SID}.jsonl", f"{ACCOUNT}\n".encode())
    assert rs.main(["--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "PreCompact snapshots" in out and "1 account-id" in out and ACCOUNT not in out
