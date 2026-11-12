"""The benchmark snapshot: the sessions and checkpoints before a cut, and a bundle at the cut commit."""

import json
import sys
from pathlib import Path

import pytest

from conftest import run_git

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "acceptance"))
import benchmark as bm  # noqa: E402
import snapshot_subject as ss  # noqa: E402

from wscontext.config import Config  # noqa: E402
from wscontext.store.paths import StorePaths  # noqa: E402
from wscontext.store.records import CheckpointRecord, SessionRecord, write_checkpoint, write_session  # noqa: E402

OLD, MID, LATE, CUT, AFTER = "s-old", "s-mid", "s-late", "s-cut", "s-after"
CTX = {"cwd": "/w", "version": "2.1.0", "gitBranch": "main", "entrypoint": "cli", "userType": "external"}


def transcript(sid: str, stamp: str, sidechain: bool = False) -> bytes:
    ctx = {**CTX, "sessionId": sid, "timestamp": stamp, **({"isSidechain": True, "agentId": "a1"} if sidechain else {})}
    return json.dumps({"type": "user", "uuid": "u0", "message": {"role": "user", "content": "go"}, **ctx}).encode() + b"\n"


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    run_git(repo, "add", name)
    run_git(repo, "commit", "-q", "-m", name)
    return run_git(repo, "rev-parse", "HEAD").strip()


def session(store: StorePaths, sid: str, started: str, ended: str | None, last: str, sub_last: str | None = None) -> None:
    """A session record, its transcript whose last entry is at `last`, a snapshot, and a subagent file."""
    write_session(store, SessionRecord(session_id=sid, author="a@b.c", started_at=started, ended_at=ended), Config())
    raw = store.raw / "sessions"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / f"2026-11-0{started[9]}-{sid}.jsonl").write_bytes(transcript(sid, last))
    (raw / f"2026-11-0{started[9]}-pc20261101T000000Z-{sid}.jsonl").write_bytes(transcript(sid, last))
    (store.raw / "subagents" / sid).mkdir(parents=True)
    (store.raw / "subagents" / sid / "agent.jsonl").write_bytes(transcript(sid, sub_last or started, sidechain=True))


@pytest.fixture
def world(git_repo):
    store = StorePaths.resolve(git_repo)
    session(store, OLD, "2026-11-01T10:00:00+00:00", "2026-11-01T11:00:00+00:00", "2026-11-01T10:59:00Z")
    session(store, MID, "2026-11-02T10:00:00+00:00", "2026-11-02T11:00:00+00:00", "2026-11-02T10:59:00Z")
    # closed late: its record says it ended after the cut, but its last entry is before it
    session(store, LATE, "2026-11-03T10:00:00+00:00", "2026-11-05T11:00:00+00:00", "2026-11-03T12:00:00Z")
    session(store, CUT, "2026-11-04T10:00:00+00:00", None, "2026-11-04T10:00:00Z")
    session(store, AFTER, "2026-11-05T10:00:00+00:00", None, "2026-11-05T10:00:00Z")
    cut = commit(git_repo, "b.txt")
    past = commit(git_repo, "c.txt")
    older = run_git(git_repo, "rev-parse", f"{cut}~1").strip()
    for sha, ids in ((cut, [OLD, MID]), (older, [OLD, AFTER]), (past, [OLD]), ("f" * 40, [OLD])):
        write_checkpoint(store, CheckpointRecord(sha=sha, author="a@b.c", session_ids=ids), Config())
    return git_repo, store, cut, past


def snapshot(repo, out, before="s-cut", sha=None):
    return ss.main([str(repo), "--commit", sha, "--before-session", before, "--out", str(out), "--branch", "main"])


def test_a_session_before_the_cut_is_kept_with_its_raw_files_and_one_after_it_is_left_out(world, tmp_path, capsys):
    repo, store, cut, _ = world
    assert snapshot(repo, tmp_path / "out", sha=cut) == 0
    kept = tmp_path / "out" / "store"
    # LATE has an `ended_at` after the cut but no entry after it
    assert sorted(p.stem for p in (kept / "records" / "sessions").glob("*.json")) == [LATE, MID, OLD]
    assert sorted(p.name for p in (kept / "raw" / "sessions").iterdir()) == sorted(
        [f"2026-11-01-{OLD}.jsonl", f"2026-11-01-pc20261101T000000Z-{OLD}.jsonl",
         f"2026-11-02-{MID}.jsonl", f"2026-11-02-pc20261101T000000Z-{MID}.jsonl",
         f"2026-11-03-{LATE}.jsonl", f"2026-11-03-pc20261101T000000Z-{LATE}.jsonl"])
    assert sorted(p.name for p in (kept / "raw" / "subagents").iterdir()) == [LATE, MID, OLD]
    assert "3 kept" in capsys.readouterr().out


@pytest.mark.parametrize(("own", "sub"), [("2026-11-04T10:00:01Z", None), (None, "2026-11-04T10:00:00Z")])
def test_a_session_with_an_entry_at_or_after_the_cut_stops_the_snapshot(world, tmp_path, own, sub):
    repo, store, cut, _ = world
    spans = "s-span"
    session(store, spans, "2026-11-03T20:00:00+00:00", "2026-11-03T21:00:00+00:00", own or "2026-11-03T20:30:00Z",
            sub_last=sub or None)
    with pytest.raises(SystemExit, match=spans):
        snapshot(repo, tmp_path / "out", sha=cut)
    assert not (tmp_path / "out").exists()


def test_a_checkpoint_past_the_commit_is_left_out_and_one_without_kept_sessions_too(world, tmp_path):
    repo, store, cut, past = world
    snapshot(repo, tmp_path / "out", sha=cut)
    shas = {p.stem for p in (tmp_path / "out" / "store" / "records" / "checkpoints").glob("*.json")}
    assert past not in shas and "f" * 40 not in shas
    # the one naming a kept and a later session survives with the kept one only
    older = run_git(repo, "rev-parse", f"{cut}~1").strip()
    assert shas == {cut, older}
    rec = json.loads((tmp_path / "out" / "store" / "records" / "checkpoints" / f"{older}.json").read_text())
    assert rec["session_ids"] == [OLD]


def test_a_checkpoint_naming_only_later_sessions_is_left_out(world, tmp_path):
    repo, store, cut, _ = world
    write_checkpoint(store, CheckpointRecord(sha=cut, author="a@b.c", session_ids=[AFTER]), Config())
    snapshot(repo, tmp_path / "out", sha=cut)
    assert not (tmp_path / "out" / "store" / "records" / "checkpoints" / f"{cut}.json").exists()


def test_the_bundle_checks_out_at_the_commit_with_one_branch(world, tmp_path):
    repo, _, cut, past = world
    snapshot(repo, tmp_path / "out", sha=cut)
    clone = tmp_path / "clone"
    run_git(tmp_path, "clone", "-q", "--branch", "main", str(tmp_path / "out" / "repo.bundle"), str(clone))
    assert run_git(clone, "rev-parse", "HEAD").strip() == cut
    assert past not in run_git(clone, "rev-list", "--all").split()
    assert run_git(repo, "rev-parse", "HEAD").strip() == past  # the repository's branch did not move


def test_the_subject_loads_with_the_runners_loader(world, tmp_path):
    repo, _, cut, _ = world
    snapshot(repo, tmp_path / "out", sha=cut)
    subject = bm.load_subject(tmp_path / "out")
    assert subject.bundle.is_file() and (subject.store / "records").is_dir() and (subject.store / "raw").is_dir()
    assert (subject.suite, subject.setup, subject.tools) == (".venv/bin/pytest -q -x", "uv-venv", "Bash(.venv/bin/pytest:*)")
    assert subject.branch == "main"


def test_a_session_prefix_must_match_one_session_and_a_used_folder_is_refused(world, tmp_path):
    repo, _, cut, _ = world
    with pytest.raises(SystemExit, match="0 stored sessions match"):
        snapshot(repo, tmp_path / "out", before="zzz", sha=cut)
    with pytest.raises(SystemExit, match="5 stored sessions match"):
        snapshot(repo, tmp_path / "out", before="s-", sha=cut)
    (tmp_path / "used").mkdir()
    (tmp_path / "used" / "x").write_text("")
    with pytest.raises(SystemExit, match="not empty"):
        snapshot(repo, tmp_path / "used", sha=cut)
