from support import hook, transcript  # noqa: F401  (fixtures)
import json
import subprocess

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
