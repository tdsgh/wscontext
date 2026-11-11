from support import hook, transcript  # noqa: F401  (fixtures)
import gc
import json
import subprocess
import time
import warnings

from wscontext.history.branch import ls_blobs, read_tip
from wscontext.store.records import read_session


def test_full_session(hook, git_repo):
    store = hook.store
    entries = [
        hook("SessionStart", "session_start"),
        hook("UserPromptSubmit", "user_prompt_submit"),
        hook("PostToolUse", "post_tool_use_read"),
        hook("PostToolUse", "post_tool_use_edit"),
        hook("PostToolUse", "post_tool_use_ctx"),
        hook("Stop", "stop"),
    ]
    assert all(e["ok"] for e in entries), entries
    active = json.loads((store.state / "active.json").read_text())
    assert list(active[str(store.root)]) == ["sess-cap1"]
    raw = list((store.raw / "sessions").glob("*-sess-cap1.jsonl"))
    assert len(raw) == 1 and b"ghp_" not in raw[0].read_bytes()
    assert b"[REDACTED:" in raw[0].read_bytes()

    assert hook("SessionEnd", "session_end")["ok"]
    assert json.loads((store.state / "active.json").read_text()) == {}
    rec = read_session(store, "sess-cap1")
    assert rec.ended_at and rec.started_at and rec.branch == "main" and rec.head_start
    assert rec.tool_calls == {"read": 1, "edit": 1, "ctx": 1}
    assert rec.ctx_calls == {"why": 1}
    assert rec.files_touched == ["a.txt"]
    assert rec.subagents == ["abc123"]
    assert len(rec.turns) == 3 and rec.turns[0].tool_calls == {"read": 2, "agent": 1}
    assert rec.tokens and rec.tokens["input"] > 0
    assert (store.raw / "subagents" / "sess-cap1" / "abc123.jsonl").exists()
    # the committed history holds the redacted copy and the record
    paths = set(ls_blobs(store, read_tip(store)))
    assert "records/sessions/sess-cap1.json" in paths
    assert f"raw/sessions/{raw[0].name}" in paths and "raw/subagents/sess-cap1/abc123.jsonl" in paths
    assert not (store.state / "turns" / "sess-cap1.json").exists()


def test_hook_log_and_ctx_calls_log(hook):
    hook("SessionStart", "session_start")
    hook("PostToolUse", "post_tool_use_ctx")
    lines = [json.loads(x) for x in (hook.store.logs / "hooks.jsonl").read_text().splitlines()]
    assert [(x["event"], x["ok"]) for x in lines] == [("SessionStart", True), ("PostToolUse", True)]
    call = json.loads((hook.store.logs / "ctx-calls.jsonl").read_text())
    assert call["argv"] == ["why", "src/a.py:10"] and call["session_id"] == "sess-cap1"
    assert call["output_bytes"] == len("answer text")


def test_turn_times_survive_in_state(hook):
    hook("UserPromptSubmit", "user_prompt_submit")
    hook("Stop", "stop")
    state = json.loads((hook.store.state / "turns" / "sess-cap1.json").read_text())
    assert state["turns"][0]["started_at"] and state["turns"][0]["ended_at"]


def test_resume_keeps_session_record(hook):
    hook("SessionStart", "session_start")
    first = read_session(hook.store, "sess-cap1")
    hook("SessionStart", "session_start")
    assert read_session(hook.store, "sess-cap1") == first


def test_precompact_snapshot_follows_flag(hook, git_repo):
    (git_repo / ".wscontext").mkdir()
    (git_repo / ".wscontext" / "config.toml").write_text("[capture]\nprecompact_snapshot = true\n")
    hook("PreCompact", "pre_compact")
    assert len(list((hook.store.raw / "sessions").glob("*-pc*-sess-cap1.jsonl"))) == 1


def test_precompact_flag_off(hook, git_repo):
    (git_repo / ".wscontext").mkdir()
    (git_repo / ".wscontext" / "config.toml").write_text("[capture]\nprecompact_snapshot = false\n")
    assert hook("PreCompact", "pre_compact")["ok"]
    assert not (hook.store.raw / "sessions").exists()


def test_unsupported_format_degrades_to_raw_copy(hook, transcript):
    transcript.write_text('{"type":"user","version":"9.0.0","message":{"content":"hi"}}\n')
    hook("UserPromptSubmit", "user_prompt_submit")
    hook("Stop", "stop")
    hook("SessionStart", "session_start")
    assert hook("SessionEnd", "session_end")["ok"]
    rec = read_session(hook.store, "sess-cap1")
    assert rec.ended_at and len(rec.turns) == 1 and rec.turns[0].tool_calls == {}
    assert list((hook.store.raw / "sessions").glob("*-sess-cap1.jsonl"))


def test_no_secret_in_a_branch_name_reaches_disk(hook, git_repo):
    secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    subprocess.run(["git", "checkout", "-q", "-b", f"feat/{secret}"], cwd=git_repo, check=True)
    for event, fixture in [("SessionStart", "session_start"), ("UserPromptSubmit", "user_prompt_submit"),
                           ("PostToolUse", "post_tool_use_edit"), ("Stop", "stop"),
                           ("SessionEnd", "session_end")]:
        assert hook(event, fixture)["ok"]
    store = hook.store
    assert read_session(store, "sess-cap1").branch == "feat/[REDACTED:github-token]"
    for path in store.local.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path
    for rel, (_, sha, _) in ls_blobs(store, read_tip(store)).items():
        assert secret.encode() not in subprocess.run(
            ["git", "cat-file", "blob", sha], cwd=git_repo, capture_output=True).stdout, rel


def test_no_account_identifier_reaches_a_stored_copy(hook, git_repo, transcript):
    owner = {"type": "session-owner", "sessionId": "sess-cap1",
             "accountUuid": "00000000-0000-4000-8000-0000000000a1",
             "organizationUuid": "00000000-0000-4000-8000-0000000000b2"}
    org = {"type": "attachment", "sessionId": "sess-cap1",
           "attachment": {"type": "credential_org", "organizationUuid": owner["organizationUuid"]}}
    for path in (transcript, transcript.parent / "sess-cap1" / "subagents" / "agent-abc123.jsonl"):
        with path.open("a") as f:
            f.write(json.dumps(owner) + "\n" + json.dumps({**org, "isSidechain": True}) + "\n")
    for event, fixture in [("SessionStart", "session_start"), ("UserPromptSubmit", "user_prompt_submit"),
                           ("Stop", "stop"), ("SessionEnd", "session_end")]:
        assert hook(event, fixture)["ok"]
    store = hook.store
    assert (store.raw / "subagents" / "sess-cap1" / "abc123.jsonl").exists()
    for path in store.local.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert b"accountUuid" not in data and b"organizationUuid" not in data, path
    for rel, (_, sha, _) in ls_blobs(store, read_tip(store)).items():
        blob = subprocess.run(["git", "cat-file", "blob", sha], cwd=git_repo, capture_output=True).stdout
        assert b"accountUuid" not in blob and b"organizationUuid" not in blob, rel


# --- stage 2: reduced copies, idle finalization, local pruning ---

import os
from datetime import datetime, timedelta, timezone

from wscontext.adapters.claude_code import measure
from wscontext.capture import dispatch, worker
from wscontext.capture.state import active_path, read_json, turn_state_path, update_json, worktree_key
from wscontext.config import Config, load_config
from wscontext.githooks.handlers import link_sessions
from wscontext.history.branch import read_blob
from wscontext.history.sync import materialize
from wscontext.store.records import write_session


def make_idle(store, sid: str, hours: float = 3) -> datetime:
    """Backdate a session's start and its last prompt or tool call."""
    at = datetime.now(timezone.utc) - timedelta(hours=hours)
    update_json(active_path(store),
                lambda d: d.setdefault(worktree_key(store, Config()), {}).__setitem__(
                    sid, at.isoformat(timespec="seconds")), Config())
    turn = turn_state_path(store, sid)
    if turn.exists():
        os.utime(turn, (at.timestamp(), at.timestamp()))
    return at


def start_other(store, sid: str = "sess-other") -> dict:
    return dispatch.run("SessionStart", json.dumps(
        {"session_id": sid, "transcript_path": "", "cwd": str(store.root), "source": "startup"}))


def run_session(hook, end: bool = True) -> None:
    for event, fixture in [("SessionStart", "session_start"), ("UserPromptSubmit", "user_prompt_submit"),
                           ("PostToolUse", "post_tool_use_read"), ("Stop", "stop")]:
        assert hook(event, fixture)["ok"]
    if end:
        assert hook("SessionEnd", "session_end")["ok"]


def worker_lines(store, event: str) -> list[dict]:
    return [e for e in map(json.loads, (store.logs / "hooks.jsonl").read_text().splitlines())
            if e["event"] == event]


def test_stored_copy_is_reduced_and_its_bytes_recorded(hook, transcript):
    with transcript.open("a") as f:
        f.write('{"type":"api-request","sessionId":"sess-cap1","params":{"system":"long"}}\n')
    run_session(hook)
    store = hook.store
    [raw] = (store.raw / "sessions").glob("*-sess-cap1.jsonl")
    assert b"api-request" not in raw.read_bytes()
    sub = store.raw / "subagents" / "sess-cap1" / "abc123.jsonl"
    rec = read_session(store, "sess-cap1")
    assert rec.ended_by == "session-end"
    source = transcript.stat().st_size + (
        transcript.parent / "sess-cap1" / "subagents" / "agent-abc123.jsonl").stat().st_size
    sizes = rec.transcript_bytes
    assert sizes["source"] == source
    assert sizes["stored"] == raw.stat().st_size + sub.stat().st_size < source
    assert sum(v for k, v in sizes.items() if k not in {"source", "stored"}) == sizes["stored"]
    # sidechain lines in the main copy and the subagent copies both count as `subagent`
    assert sizes["subagent"] == measure(raw)["subagent"] + sub.stat().st_size
    assert sizes["prompt"] > 0


def test_turn_state_records_the_transcript(hook, transcript):
    hook("UserPromptSubmit", "user_prompt_submit")
    state = json.loads(turn_state_path(hook.store, "sess-cap1").read_text())
    assert state["transcript"] == str(transcript)


def test_abandoned_session_is_finalized_by_sweep_then_resumed(hook, git_repo):
    store = hook.store
    run_session(hook, end=False)
    idle_since = make_idle(store, "sess-cap1")
    assert start_other(store)["ok"]  # the next session start sweeps

    rec = read_session(store, "sess-cap1")
    assert rec.ended_by == "idle" and rec.ended_at == idle_since.isoformat(timespec="seconds")
    assert rec.tool_calls == {"read": 1} and len(rec.turns) == 3 and rec.transcript_bytes["source"]
    paths = set(ls_blobs(store, read_tip(store)))
    [raw] = (store.raw / "sessions").glob("*-sess-cap1.jsonl")
    assert {"records/sessions/sess-cap1.json", f"raw/sessions/{raw.name}"} <= paths
    assert list(json.loads(active_path(store).read_text())[str(store.root)]) == ["sess-other"]
    assert not turn_state_path(store, "sess-cap1").exists()
    [line] = worker_lines(store, "worker:sweep")
    assert line["ok"] and line["finalized"] == ["sess-cap1"]

    # resumed: active again, its new counts add to the earlier ones, its commits are linked
    hook("SessionStart", "session_start")
    hook("PostToolUse", "post_tool_use_edit")
    (git_repo / "b.txt").write_text("b\n")
    subprocess.run(["git", "add", "b.txt"], cwd=git_repo, check=True)
    message = git_repo / ".git" / "MSG"
    message.write_text("add b\n")
    link_sessions(store, load_config(git_repo), [str(message)], "")
    subprocess.run(["git", "commit", "-q", "-F", str(message)], cwd=git_repo, check=True)
    out = subprocess.run(["git", "log", "-1", "--format=%(trailers:key=AI-Session,valueonly)"],
                         cwd=git_repo, capture_output=True, text=True, check=True).stdout
    assert "sess-cap1" in out.split()
    assert hook("SessionEnd", "session_end")["ok"]
    rec = read_session(store, "sess-cap1")
    assert rec.tool_calls == {"read": 1, "edit": 1} and rec.ended_by == "session-end"


def test_a_prompt_after_idle_finalization_makes_the_session_active_again(hook):
    store = hook.store
    run_session(hook, end=False)
    make_idle(store, "sess-cap1")
    worker.run("sweep", "", "", str(store.root))
    assert "sess-cap1" not in read_json(active_path(store)).get(str(store.root), {})
    hook("UserPromptSubmit", "user_prompt_submit")
    assert "sess-cap1" in read_json(active_path(store))[str(store.root)]


def test_sweep_finalizes_from_the_raw_copy_when_the_transcript_is_gone(hook, transcript):
    store = hook.store
    run_session(hook, end=False)
    transcript.unlink()
    make_idle(store, "sess-cap1")
    worker.run("sweep", "", "", str(store.root))
    rec = read_session(store, "sess-cap1")
    assert rec.ended_by == "idle" and len(rec.turns) == 3 and rec.subagents == ["abc123"]
    assert "source" not in rec.transcript_bytes and rec.transcript_bytes["stored"] > 0


def test_sweep_leaves_fresh_sessions_and_skips_while_another_runs(hook):
    store = hook.store
    run_session(hook, end=False)
    worker.run("sweep", "", "", str(store.root))
    assert read_session(store, "sess-cap1").ended_at is None
    make_idle(store, "sess-cap1")
    lock = store.state / "locks" / "sweep.lock"
    lock.write_text("held\n")
    worker.run("sweep", "", "", str(store.root))
    assert read_session(store, "sess-cap1").ended_at is None
    lock.unlink()
    worker.run("sweep", "", "", str(store.root))
    assert read_session(store, "sess-cap1").ended_by == "idle"


def test_session_start_spawns_sweep_when_due(hook, monkeypatch):
    store = hook.store
    calls: list[list[str]] = []
    monkeypatch.setattr(worker, "spawn_worker", lambda args, cwd, **kw: calls.append(args))
    hook("SessionStart", "session_start")
    assert [c[0] for c in calls] == ["sweep"]  # never swept yet
    (store.state / "last_sweep").write_text(f"{datetime.now().timestamp()}\n")
    calls.clear()
    hook("SessionStart", "session_start")
    assert calls == []
    make_idle(store, "sess-cap1")
    start_other(store)
    assert [c[0] for c in calls] == ["sweep"]


def age(store, sid: str, days: int) -> None:
    rec = read_session(store, sid)
    rec.started_at = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    write_session(store, rec, Config())


def test_old_session_is_pruned_and_stays_readable_through_the_branch(hook):
    store = hook.store
    run_session(hook)
    [raw] = (store.raw / "sessions").glob("*-sess-cap1.jsonl")
    sub = store.raw / "subagents" / "sess-cap1" / "abc123.jsonl"
    content = raw.read_bytes()
    tip = read_tip(store)
    age(store, "sess-cap1", 200)
    worker.run("sweep", "", "", str(store.root))

    rel = [f"raw/sessions/{raw.name}", "raw/subagents/sess-cap1/abc123.jsonl"]
    assert not raw.exists() and not sub.exists() and not sub.parent.exists()
    assert sorted(json.loads((store.state / "pruned.json").read_text())) == sorted(rel)
    assert read_tip(store) == tip and read_blob(store, rel[0]) == content
    assert read_session(store, "sess-cap1").turns  # records stay
    (store.state / "materialized.json").unlink(missing_ok=True)
    assert materialize(store, load_config(store.root)) == 0
    assert not raw.exists() and not sub.exists()
    [line] = worker_lines(store, "worker:sweep")
    assert sorted(line["pruned"]) == sorted(rel)
    (store.state / "pruned.json").unlink()  # without the list, materialize writes them back
    assert materialize(store, load_config(store.root)) == 2 and raw.read_bytes() == content


def test_recent_and_active_sessions_are_not_pruned(hook):
    store = hook.store
    run_session(hook)
    worker.run("sweep", "", "", str(store.root))
    assert list((store.raw / "sessions").glob("*-sess-cap1.jsonl"))
    age(store, "sess-cap1", 200)
    hook("SessionStart", "session_start")  # resumed: active again
    worker.run("sweep", "", "", str(store.root))
    assert list((store.raw / "sessions").glob("*-sess-cap1.jsonl"))
    assert not (store.state / "pruned.json").exists()


def test_with_sync_on_only_copies_on_a_shared_tip_are_pruned(hook, git_repo):
    store = hook.store
    (git_repo / ".wscontext").mkdir()
    (git_repo / ".wscontext" / "config.toml").write_text('[sync]\nremote = "origin"\n')
    run_session(hook)
    [raw] = (store.raw / "sessions").glob("*-sess-cap1.jsonl")
    age(store, "sess-cap1", 200)
    worker.run("sweep", "", "", str(store.root))
    assert raw.exists()  # never pushed or fetched: not on any shared tip
    subprocess.run(["git", "update-ref", "refs/wscontext/remotes/origin/history", read_tip(store)],
                   cwd=git_repo, check=True)
    worker.run("sweep", "", "", str(store.root))
    assert not raw.exists()


def test_a_local_copy_that_differs_from_the_branch_is_not_pruned(hook):
    store = hook.store
    run_session(hook)
    [raw] = (store.raw / "sessions").glob("*-sess-cap1.jsonl")
    raw.write_bytes(raw.read_bytes() + b'{"type":"user","message":{"content":"later"}}\n')
    age(store, "sess-cap1", 200)
    worker.run("sweep", "", "", str(store.root))
    assert raw.exists()


def test_two_ctx_calls_in_one_command_count_once_each(hook):
    hook("SessionStart", "session_start")
    hook("PostToolUse", "post_tool_use_ctx_two")
    hook("SessionEnd", "session_end")
    rec = read_session(hook.store, "sess-cap1")
    assert rec.ctx_calls == {"why#name": 1, "why": 1}
    assert rec.tool_calls == {"ctx": 1}


# --- the operator's account identifiers ---

ACCT_ID = "00000000-0000-4000-8000-0000000000c3"
ORG_ID = "00000000-0000-4000-8000-0000000000d4"
SESSION_UUID = "00000000-0000-4000-8000-0000000000e5"


def _leaky_transcript(transcript, owner_line: bool) -> None:
    lines = []
    if owner_line:
        lines.append({"type": "session-owner", "sessionId": "sess-cap1",
                      "accountUuid": ACCT_ID, "organizationUuid": ORG_ID})
    lines += [
        {"type": "user", "version": "2.0.5", "timestamp": "2026-01-01T10:03:00Z",
         "message": {"role": "user", "content": f"my account is {ACCT_ID}, session {SESSION_UUID}"}},
        {"type": "user", "version": "2.0.5", "timestamp": "2026-01-01T10:04:00Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1",
              "content": f"org {ORG_ID.upper()} and {SESSION_UUID}"}]}},
    ]
    with transcript.open("a") as f:
        f.write("".join(json.dumps(x) + "\n" for x in lines))


def _store_texts(hook, git_repo):
    store = hook.store
    texts = {str(p): p.read_bytes() for p in store.local.rglob("*") if p.is_file()}
    for rel, (_, sha, _) in ls_blobs(store, read_tip(store)).items():
        texts["branch:" + rel] = subprocess.run(["git", "cat-file", "blob", sha], cwd=git_repo,
                                                capture_output=True).stdout
    return texts


def _fire_all(hook):
    for event, fixture in [("SessionStart", "session_start"), ("UserPromptSubmit", "user_prompt_submit"),
                           ("Stop", "stop"), ("SessionEnd", "session_end")]:
        assert hook(event, fixture)["ok"]


def test_owner_line_ids_are_masked_everywhere_and_kept_as_digests(hook, git_repo, transcript):
    from wscontext.redact.rules import account_digest
    _leaky_transcript(transcript, owner_line=True)
    subprocess.run(["git", "checkout", "-q", "-b", f"feat/{ACCT_ID}"], cwd=git_repo, check=True)
    _fire_all(hook)
    texts = _store_texts(hook, git_repo)
    raw = next(v for k, v in texts.items() if "/raw/sessions/" in k)
    assert raw.count(b"[REDACTED:account-id]") == 2 and SESSION_UUID.encode() in raw
    assert read_session(hook.store, "sess-cap1").branch == "feat/[REDACTED:account-id]"
    for name, data in texts.items():
        for secret in (ACCT_ID, ORG_ID):
            assert secret.encode() not in data.lower(), name
    state = json.loads((hook.store.state / "account_ids.json").read_text())
    assert set(state["sha256"]) == {account_digest(ACCT_ID), account_digest(ORG_ID)}
    log = (hook.store.logs / "hooks.jsonl").read_text()
    assert ACCT_ID not in log and ORG_ID not in log


def test_account_file_ids_are_masked_without_an_owner_line(hook, git_repo, transcript, tmp_path,
                                                           monkeypatch):
    from wscontext.redact import account
    file = tmp_path / "account.json"
    file.write_text(json.dumps({"oauthAccount": {"accountUuid": ACCT_ID,
                                                 "organizationUuid": ORG_ID}}))
    monkeypatch.setattr(account, "account_file_path", lambda: file)
    _leaky_transcript(transcript, owner_line=False)
    _fire_all(hook)
    texts = _store_texts(hook, git_repo)
    for name, data in texts.items():
        for secret in (ACCT_ID, ORG_ID):
            assert secret.encode() not in data.lower(), name
    raw = next(v for k, v in texts.items() if "/raw/sessions/" in k)
    assert raw.count(b"[REDACTED:account-id]") == 2 and SESSION_UUID.encode() in raw


def test_without_any_account_source_nothing_is_masked_and_hooks_succeed(hook, git_repo, transcript):
    _leaky_transcript(transcript, owner_line=False)
    _fire_all(hook)
    raw = next(v for k, v in _store_texts(hook, git_repo).items() if "/raw/sessions/" in k)
    assert ACCT_ID.encode() in raw and b"account-id" not in raw


def test_a_started_worker_leaves_no_process_handle_behind(tmp_path):
    (tmp_path / "lingering.py").write_text(
        "import pathlib, time\n"
        "pathlib.Path('started').write_text('1')\n"
        "while not pathlib.Path('stop').exists():\n"
        "    time.sleep(0.05)\n")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        worker.spawn_worker([], tmp_path, module="lingering")
        deadline = time.monotonic() + 10
        while not (tmp_path / "started").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        gc.collect()  # the dropped Popen object is collected while the process still runs
    (tmp_path / "stop").write_text("1")
    assert (tmp_path / "started").exists()
    assert [str(w.message) for w in caught if w.category is ResourceWarning] == []
