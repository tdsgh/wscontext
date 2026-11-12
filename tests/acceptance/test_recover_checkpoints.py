"""The one-off checkpoint recovery: commits with no record are linked to the session that committed them."""

import json
import sys
from pathlib import Path

import pytest

from conftest import put_store_file, run_git

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "acceptance"))
import recover_checkpoints as rc  # noqa: E402

from wscontext.config import Config  # noqa: E402
from wscontext.history.branch import HISTORY_REF, ls_blobs, read_tip  # noqa: E402
from wscontext.store.paths import StorePaths  # noqa: E402
from wscontext.store.records import CheckpointRecord, read_checkpoint, write_checkpoint  # noqa: E402

CTX = {"cwd": "/w", "version": "2.1.0", "gitBranch": "main", "entrypoint": "cli", "userType": "external"}


def _line(obj: dict) -> bytes:
    return json.dumps(obj).encode() + b"\n"


def transcript(sid: str, calls: list[tuple[str, str]], stamp: str = "2026-11-06T10:00:00Z",
               sidechain: bool = False, agent: str = "a1") -> bytes:
    """A transcript with one turn holding Bash calls: (command, result) pairs."""
    ctx = {**CTX, "sessionId": sid, "timestamp": stamp, **({"isSidechain": True, "agentId": agent} if sidechain else {})}
    lines = [{"type": "user", "uuid": "u0", "message": {"role": "user", "content": "go"}, **ctx}]
    for n, (command, result) in enumerate(calls):
        lines.append({"type": "assistant", "uuid": f"a{n}", **ctx, "message": {
            "role": "assistant", "id": f"m{n}",
            "content": [{"type": "tool_use", "id": f"t{n}", "name": "Bash", "input": {"command": command}}]}})
        lines.append({"type": "user", "uuid": f"r{n}", **ctx, "message": {
            "role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{n}", "content": result}]}})
    return b"".join(_line(o) for o in lines)


@pytest.fixture
def repo(git_repo):
    """A repo whose first commit (`init`) no session ran."""
    return git_repo


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    run_git(repo, "add", name)
    run_git(repo, "commit", "-q", "-m", f"add {name}")
    return run_git(repo, "rev-parse", "HEAD").strip()


def add_main(store: StorePaths, sid: str, data: bytes) -> None:
    put_store_file(store.local, f"raw/sessions/2026-11-06-{sid}.jsonl", data)


def run(repo: Path, tmp_path: Path, *extra: str) -> int:
    return rc.main([str(repo), "--branch", "main", "--backup", str(tmp_path / "backup"), *extra])


def test_a_commit_named_in_a_git_commit_result_gets_a_record_for_that_session(repo, tmp_path):
    store = StorePaths.resolve(repo)
    root = run_git(repo, "rev-parse", "HEAD").strip()
    sha = commit(repo, "b.txt")
    add_main(store, "sess-1", transcript("sess-1", [
        ("git add -A && git commit -q -m x && git log --oneline -1", f"{sha[:7]} add b.txt\n")]))
    assert run(repo, tmp_path) == 0
    rec = read_checkpoint(store, sha)
    assert rec.session_ids == ["sess-1"] and rec.author == "t@example.com"
    assert rec.created_at == run_git(repo, "log", "-1", "--format=%cI", sha).strip()
    assert (rec.files_changed, rec.insertions, rec.deletions) == (1, 1, 0)
    assert rec.rewritten_from is None
    assert not (store.records / "checkpoints" / f"{root}.json").exists()  # no session ran it


def test_an_old_sha_through_a_map_links_the_new_commit(repo, tmp_path):
    store = StorePaths.resolve(repo)
    sha = commit(repo, "b.txt")
    old = "1234567" + "0" * 33
    mapfile = tmp_path / "map.txt"
    mapfile.write_text(f"{old} {sha}\n")
    add_main(store, "sess-1", transcript("sess-1", [("git commit -q -m x; git log -1", f"{old[:7]} add b.txt\n")]))
    assert run(repo, tmp_path, "--map", str(mapfile)) == 0
    rec = read_checkpoint(store, sha)
    assert rec.session_ids == ["sess-1"] and rec.rewritten_from == old


def test_a_subagent_transcript_links_its_parent_session(repo, tmp_path):
    store = StorePaths.resolve(repo)
    sha = commit(repo, "b.txt")
    put_store_file(store.local, "raw/subagents/sess-1/a1.jsonl", transcript(
        "sess-1", [("git commit -q -m x", f"[main {sha[:7]}] add b.txt\n")], sidechain=True))
    assert run(repo, tmp_path) == 0
    assert read_checkpoint(store, sha).session_ids == ["sess-1"]


def test_with_two_sessions_the_earlier_result_wins_and_the_dry_run_marks_it(repo, tmp_path, capsys):
    store = StorePaths.resolve(repo)
    sha = commit(repo, "b.txt")
    result = f"[main {sha[:7]}] add b.txt\n"
    add_main(store, "sess-a", transcript("sess-a", [("git commit -q -m x", result)], "2026-11-06T12:00:00Z"))
    add_main(store, "sess-b", transcript("sess-b", [("git commit -q -m x", result)], "2026-11-06T11:00:00Z"))
    assert run(repo, tmp_path, "--dry-run") == 0
    out = capsys.readouterr().out
    assert f"{sha[:7]} -> sess-b  [earlier result of several sessions]" in out
    assert run(repo, tmp_path) == 0
    assert read_checkpoint(store, sha).session_ids == ["sess-b"]


def test_a_sha_named_outside_a_git_commit_call_links_nothing(repo, tmp_path, capsys):
    store = StorePaths.resolve(repo)
    sha = commit(repo, "b.txt")
    data = transcript("sess-1", [("git log --oneline -1", f"{sha[:7]} add b.txt\n"),
                                 ("git status", "nothing"), ("ls", f"{sha[:7]}")])
    # a description that says "git commit" does not make the call one
    data += transcript("sess-1", [("git log -1", f"{sha[:7]}")]).replace(
        b'"input": {"command": "git log -1"}', b'"input": {"command": "git log -1", "description": "git commit it"}')
    assert b"git commit it" in data
    add_main(store, "sess-1", data)
    assert run(repo, tmp_path) == 0
    out = capsys.readouterr().out
    assert f"  {sha[:7]}  add b.txt" in out and "0 commit(s) to link" in out
    assert not (store.records / "checkpoints" / f"{sha}.json").exists()


def test_a_long_command_and_a_late_sha_are_still_read_whole(repo, tmp_path):
    store = StorePaths.resolve(repo)
    sha = commit(repo, "b.txt")
    command = "git add " + "x" * 200 + " && git commit -q -m x"
    add_main(store, "sess-1", transcript("sess-1", [(command, "y" * 3000 + f"\n{sha[:7]} add b.txt")]))
    assert run(repo, tmp_path) == 0
    assert read_checkpoint(store, sha).session_ids == ["sess-1"]


def test_a_commit_with_a_record_is_left_alone(repo, tmp_path):
    store = StorePaths.resolve(repo)
    sha = commit(repo, "b.txt")
    path = write_checkpoint(store, CheckpointRecord(sha=sha, author="t@example.com",
                                                    session_ids=["kept"]), Config())
    before = path.read_bytes()
    add_main(store, "sess-1", transcript("sess-1", [("git commit -q -m x", f"[main {sha[:7]}] x")]))
    assert run(repo, tmp_path) == 0
    assert path.read_bytes() == before


def test_dry_run_writes_nothing(repo, tmp_path, capsys):
    store = StorePaths.resolve(repo)
    sha = commit(repo, "b.txt")
    add_main(store, "sess-1", transcript("sess-1", [("git commit -q -m x", f"[main {sha[:7]}] x")]))
    before = {p: p.read_bytes() for p in store.local.rglob("*") if p.is_file()}
    assert run(repo, tmp_path, "--dry-run") == 0
    assert "dry run: nothing written" in capsys.readouterr().out
    assert {p: p.read_bytes() for p in store.local.rglob("*") if p.is_file()} == before
    assert read_tip(store) is None and not (tmp_path / "backup").exists()


def test_a_second_run_changes_nothing_and_the_history_gains_one_commit(repo, tmp_path):
    store = StorePaths.resolve(repo)
    shas = [commit(repo, "b.txt"), commit(repo, "c.txt")]
    add_main(store, "sess-1", transcript("sess-1", [
        ("git commit -q -m x", f"[main {shas[0][:7]}] b"), ("git commit -q -m y", f"[main {shas[1][:7]}] c")]))
    assert run(repo, tmp_path) == 0
    tip = read_tip(store)
    assert run_git(repo, "rev-list", "--count", HISTORY_REF).strip() == "1"
    assert {p for p in ls_blobs(store, tip)} == {f"records/checkpoints/{s}.json" for s in shas}
    assert (tmp_path / "backup" / "history-ref.txt").read_text().strip() == "none"
    files = {p: p.read_bytes() for p in store.records.rglob("*") if p.is_file()}
    assert rc.main([str(repo), "--branch", "main", "--backup", str(tmp_path / "backup2")]) == 0
    assert read_tip(store) == tip and not (tmp_path / "backup2").exists()
    assert {p: p.read_bytes() for p in store.records.rglob("*") if p.is_file()} == files
