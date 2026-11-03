import json

from cli_support import add_session, checkpoint, run_json, spawned, world  # noqa: F401
from conftest import run_git

from wscontext.capture.state import active_path, update_json, worktree_key
from wscontext.cli.main import main
from wscontext.config import Config, SyncConfig
from wscontext.history import sync
from wscontext.history.branch import commit_paths, ls_blobs, read_tip
from wscontext.store.paths import StorePaths

ON = Config(sync=SyncConfig(remote="origin", fetch_throttle_seconds=0))


def _subagent(store, sid: str):
    path = store.raw / "subagents" / sid / "agent-1.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"type": "user"}\n')
    return path


def _history_has_blob(repo, text: str) -> bool:
    """Whether any object reachable from the history branch contains `text`."""
    out = run_git(repo, "rev-list", "--objects", "wscontext/history")
    for line in out.splitlines():
        sha = line.split()[0]
        if run_git(repo, "cat-file", "-t", sha).strip() == "blob" \
                and text in run_git(repo, "cat-file", "-p", sha):
            return True
    return False


def _forgettable(world):
    """sess-one with a subagent file, a checkpoint of its own and one shared with sess-two."""
    store = world.store
    sub = _subagent(store, "sess-one")
    other = add_session(store, "sess-two", started="2026-02-01T10:00:00Z")
    solo = checkpoint(store, world.c1, ["sess-one"])
    both = checkpoint(store, world.c2, ["sess-one", "sess-two"])
    commit_paths(store, [sub, *other, solo, both], "more")
    return solo, both


def test_forget_before_a_push_removes_files_links_and_branch_history(world, capsys):
    solo, both = _forgettable(world)
    store = world.store
    assert _history_has_blob(world.repo, "add a retry to the fetcher")
    code, out, _ = world.ctx(capsys, "forget", "sess-one")
    assert code == 0
    assert "Forgot session sess-one" in out and "AI-Session trailer" in out
    assert not (store.records / "sessions" / "sess-one.json").exists()
    assert not list((store.raw / "sessions").glob("*sess-one*"))
    assert not (store.raw / "subagents" / "sess-one").exists()
    assert not solo.exists()
    assert json.loads(both.read_text())["session_ids"] == ["sess-two"]
    tip = read_tip(store)
    on_branch = ls_blobs(store, tip)
    assert not [p for p in on_branch if "sess-one" in p]
    assert f"records/checkpoints/{world.c1}.json" not in on_branch
    assert f"records/checkpoints/{world.c2}.json" in on_branch
    assert "records/sessions/sess-two.json" in on_branch
    # the local-only commits are replaced by one
    assert run_git(world.repo, "rev-list", "--count", "wscontext/history").strip() == "1"
    code, _, err = world.ctx(capsys, "show", "sess-one")
    assert code == 1 and "no session" in err


def test_forgotten_transcript_is_unreachable_from_the_branch(world, capsys):
    assert _history_has_blob(world.repo, "now add a test")
    world.ctx(capsys, "forget", "sess-one")
    assert not _history_has_blob(world.repo, "now add a test")


def test_dry_run_changes_nothing(world, capsys):
    _forgettable(world)
    tip = read_tip(world.store)
    data = run_json(world, capsys, "forget", "sess-one", "--dry-run")
    assert data["forgotten"] is False and data["dry_run"] is True
    assert "records/sessions/sess-one.json" in data["removed"]
    assert "raw/subagents/sess-one" in data["removed"]
    assert data["checkpoints_removed"] == [world.c1] and data["checkpoints_updated"] == [world.c2]
    assert read_tip(world.store) == tip
    assert (world.store.records / "sessions" / "sess-one.json").exists()


def test_forget_json_reports_what_was_removed(world, capsys):
    data = run_json(world, capsys, "forget", "sess-one")
    assert data["forgotten"] is True
    assert any(p.startswith("raw/sessions/") and p.endswith("-sess-one.jsonl") for p in data["removed"])
    assert data["notes"]


def test_forget_does_not_touch_a_session_whose_id_ends_the_same(world, capsys):
    paths = add_session(world.store, "x-sess-one")
    commit_paths(world.store, paths, "x")
    world.ctx(capsys, "forget", "sess-one")
    assert (world.store.records / "sessions" / "x-sess-one.json").exists()
    assert list((world.store.raw / "sessions").glob("*-x-sess-one.jsonl"))


def test_forget_refuses_a_session_already_pushed(shared_remote, capsys, monkeypatch):
    bare, a, _ = shared_remote
    monkeypatch.chdir(a)
    store = StorePaths.resolve(a)
    commit_paths(store, add_session(store, "sess-one"), "one")
    assert sync.push(store, ON)
    capsys.readouterr()
    assert main(["forget", "sess-one"]) == 1
    err = capsys.readouterr().err
    assert "already on the shared history" in err and "force-push" in err
    assert (store.records / "sessions" / "sess-one.json").exists()
    assert "records/sessions/sess-one.json" in ls_blobs(store, read_tip(store))
    assert main(["forget", "sess-one", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["shared"] is True and "records/sessions/sess-one.json" in data["paths"]


def test_forget_refuses_a_teammates_session(shared_remote, capsys,
                                                                         monkeypatch):
    bare, a, b = shared_remote
    sa, sb = StorePaths.resolve(a), StorePaths.resolve(b)
    commit_paths(sa, add_session(sa, "sess-a"), "a")
    sync.push(sa, ON)
    sync.fetch(sb, ON, force=True)
    monkeypatch.chdir(b)
    assert (sb.records / "sessions" / "sess-a.json").exists()
    capsys.readouterr()
    assert main(["forget", "sess-a"]) == 1
    assert "already on the shared history" in capsys.readouterr().err
    assert (sb.records / "sessions" / "sess-a.json").exists()


def test_forgetting_an_unshared_session_after_a_fetch_keeps_it_gone(shared_remote, capsys,
                                                                    monkeypatch):
    bare, a, b = shared_remote
    sa, sb = StorePaths.resolve(a), StorePaths.resolve(b)
    commit_paths(sa, add_session(sa, "sess-x"), "x")
    commit_paths(sb, add_session(sb, "sess-b"), "b")
    assert sync.push(sb, ON)
    assert sync.fetch(sa, ON, force=True)  # a's branch now holds both; materialize recorded both
    seen = sa.state / "materialized.json"
    assert "records/sessions/sess-x.json" in json.loads(seen.read_text())
    monkeypatch.chdir(a)
    capsys.readouterr()
    assert main(["forget", "sess-x"]) == 0
    entries = json.loads(seen.read_text())
    assert not [k for k in entries if "sess-x" in k]
    assert "records/sessions/sess-b.json" in entries
    # the remote moves on, a fetches again: nothing of sess-x comes back
    commit_paths(sb, add_session(sb, "sess-c", started="2026-03-01T10:00:00Z"), "c")
    assert sync.push(sb, ON)
    assert sync.fetch(sa, ON, force=True)
    assert sync.materialize(sa, ON) == 0
    assert not (sa.records / "sessions" / "sess-x.json").exists()
    assert not list((sa.raw / "sessions").glob("*sess-x*"))
    assert [p for p in ls_blobs(sa, read_tip(sa)) if "sess-x" in p] == []
    assert (sa.records / "sessions" / "sess-c.json").exists()
    # pushing sends neither sess-x nor any earlier local commit that held it
    assert sync.push(sa, ON)
    remote_paths = ls_blobs(sa, read_tip(sa, "refs/remotes/origin/wscontext/history"))
    assert not [p for p in remote_paths if "sess-x" in p]


def test_a_push_after_forget_does_not_send_the_forgotten_blob(shared_remote, capsys, monkeypatch):
    bare, a, _ = shared_remote
    sa = StorePaths.resolve(a)
    commit_paths(sa, add_session(sa, "sess-keep"), "keep")
    sync.push(sa, ON)
    secret = add_session(sa, "sess-leak", transcript=False)
    raw = sa.raw / "sessions" / "2026-01-01-sess-leak.jsonl"
    raw.write_text('{"text": "UNIQUE-LEAK-MARKER"}\n')
    commit_paths(sa, [*secret, raw], "leak")
    monkeypatch.chdir(a)
    capsys.readouterr()
    assert main(["forget", "sess-leak"]) == 0
    assert sync.push(sa, ON)
    out = run_git(bare, "rev-list", "--objects", "wscontext/history")
    for line in out.splitlines():
        sha = line.split()[0]
        if run_git(bare, "cat-file", "-t", sha).strip() == "blob":
            assert "UNIQUE-LEAK-MARKER" not in run_git(bare, "cat-file", "-p", sha)
    assert "records/sessions/sess-keep.json" in ls_blobs(sa, read_tip(sa))


def test_forget_refuses_an_active_session(world, capsys):
    update_json(active_path(world.store),
                lambda d: d.setdefault(worktree_key(world.store, Config()), {})
                .__setitem__("sess-one", "2026-01-01T10:00:00Z"), Config())
    code, _, err = world.ctx(capsys, "forget", "sess-one")
    assert code == 1 and "still active" in err
    assert (world.store.records / "sessions" / "sess-one.json").exists()


def test_forget_unknown_or_unsafe_id(world, capsys):
    code, _, err = world.ctx(capsys, "forget", "nope")
    assert code == 1 and "no session" in err
    code, _, err = world.ctx(capsys, "forget", "../x")
    assert code == 1 and "not a session ID" in err
