import dataclasses
import hashlib
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from conftest import run_git

from wscontext.adapters import claude_home
from wscontext.capture import importer, worker
from wscontext.capture.importer import find, run
from wscontext.config import Config, RetentionConfig, SyncConfig, load_config
from wscontext.githooks import worker as githooks_worker
from wscontext.history import sync
from wscontext.history.branch import HISTORY_REF, commit_paths, ls_blobs, read_tip
from wscontext.index.db import connect
from wscontext.index.search import search
from wscontext.store.paths import StorePaths
from wscontext.store.records import (CheckpointRecord, SessionRecord, read_checkpoint,
                                     read_session, write_checkpoint, write_session)

SECRET = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
ACCT_ID = "00000000-0000-4000-8000-0000000000c3"
ORG_ID = "00000000-0000-4000-8000-0000000000d4"


class Script:
    """A hand-written Claude Code transcript, one line per call, a minute apart."""

    def __init__(self, sid: str, cwd: Path | str, *, branch: str = "main",
                 version: str = "2.0.5", start: str = "2026-01-01T10:00:00+00:00",
                 agent: str | None = None):
        self.sid, self.cwd, self.branch, self.version, self.agent = sid, str(cwd), branch, version, agent
        self.at = datetime.fromisoformat(start)
        self.lines: list[dict] = []

    def add(self, kind: str, message: dict | None = None, **extra) -> "Script":
        obj = {"type": kind, "uuid": f"u{len(self.lines)}", "sessionId": self.sid, "cwd": self.cwd,
               "gitBranch": self.branch, "version": self.version,
               "timestamp": self.at.isoformat().replace("+00:00", "Z")}
        if self.agent:
            obj |= {"isSidechain": True, "agentId": self.agent}
        if message is not None:
            obj["message"] = message
        self.lines.append(obj | extra)
        self.at += timedelta(minutes=1)
        return self

    def prompt(self, text: str) -> "Script":
        return self.add("user", {"role": "user", "content": text})

    def reply(self, text: str, usage: dict | None = None) -> "Script":
        message = {"id": f"m{len(self.lines)}", "role": "assistant",
                   "content": [{"type": "text", "text": text}]}
        return self.add("assistant", message | ({"usage": usage} if usage else {}))

    def tool(self, name: str, given: dict, result: str, **extra) -> "Script":
        tid = f"t{len(self.lines)}"
        self.add("assistant", {"id": f"m{len(self.lines)}", "role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": name, "input": given}]})
        return self.add("user", {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": result}]}, **extra)

    def bash(self, command: str, result: str) -> "Script":
        return self.tool("Bash", {"command": command}, result)

    def write(self, folder: str = "-proj", age_days: float = 3, path: Path | None = None) -> Path:
        path = path or claude_home.projects_dir() / folder / f"{self.sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(x) + "\n" for x in self.lines))
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path


def git_commit(repo: Path, name: str, message: str, *extra: str) -> tuple[str, str]:
    """Commit a change to `name`; returns the new SHA and Git's own output."""
    file = repo / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(file.read_text() + "x\n" if file.exists() else "x\n")
    run_git(repo, "add", name)
    out = subprocess.run(["git", "commit", "-m", message, *extra], cwd=repo, check=True,
                         capture_output=True, text=True).stdout
    return run_git(repo, "rev-parse", "HEAD").strip(), out


def session(sid: str, cwd: Path, *, commits: tuple[str, ...] = (), prompt: str = "add a retry",
            start: str = "2026-01-01T10:00:00+00:00", **kw) -> Script:
    s = Script(sid, cwd, start=start, **kw).prompt(prompt).reply("Looking.")
    for out in commits:
        s.bash("git add -A && git commit -m 'work'", out)
    return s


@pytest.fixture
def store(git_repo: Path) -> StorePaths:
    return StorePaths.resolve(git_repo)


def ids(offer) -> list[str]:
    return [s.transcript.session_id for s in offer.sessions]


def snapshot(store: StorePaths) -> dict:
    files = {}
    for top in (store.local.parent, store.root / ".wscontext"):
        for p in top.rglob("*") if top.exists() else ():
            if p.is_file():
                files[str(p)] = (p.read_bytes(), p.stat().st_mtime_ns)
            else:
                files[str(p)] = None
    return files | {"ref": read_tip(store)}


def tip_paths(store: StorePaths) -> set[str]:
    tip = read_tip(store)
    return set(ls_blobs(store, tip)) if tip else set()


def take_all(store, config=None, **kw):
    config = config or load_config(store.root)
    offer = find(store, config, **kw)
    return run(store, config, offer, offer.items, [])


# --- preview ---

def test_preview_writes_nothing(store, git_repo, tmp_path):
    sha, out = git_commit(git_repo, "b.txt", "add b")
    session("sess-new", git_repo, commits=(out,)).write()
    live = session("sess-live", git_repo)
    live.write()
    commit_paths(store, [write_session(store, SessionRecord(session_id="sess-live", author="t"),
                                       Config())], "live")
    (git_repo / "docs").mkdir()
    (git_repo / "docs" / "adr.md").write_text("# ADR\n\nbody\n")
    run_git(git_repo, "add", "docs")
    run_git(git_repo, "commit", "-q", "-m", "docs")
    home = claude_home.config_dir()
    before = snapshot(store), {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    offer = find(store, Config())
    find(store, Config(), include_declined=True)
    after = snapshot(store), {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    assert ids(offer) == ["sess-new"] and offer.sessions[0].commits == [(sha, "add b")]
    assert [d.path for d in offer.documents] == ["docs/adr.md"]
    assert before == after


# --- found (§ 4) ---

def test_found_in_the_root_a_subfolder_and_an_outside_worktree(store, git_repo, tmp_path):
    (git_repo / "src").mkdir()
    outside = tmp_path / "wt-out"
    run_git(git_repo, "worktree", "add", "-q", "-b", "wt", str(outside))
    session("sess-root", git_repo, start="2026-01-01T10:00:00+00:00").write()
    session("sess-sub", git_repo / "src", start="2026-01-02T10:00:00+00:00").write("-sub")
    session("sess-wt", outside, start="2026-01-03T10:00:00+00:00").write("-wt")
    offer = find(store, Config())
    assert ids(offer) == ["sess-wt", "sess-sub", "sess-root"]  # newest first
    folders = {s.transcript.session_id: (s.folder, s.worktree) for s in offer.sessions}
    assert folders == {"sess-root": (None, str(git_repo)),
                       "sess-sub": ("src", str(git_repo)),
                       "sess-wt": (str(outside), str(outside))}


def test_a_removed_worktree_inside_the_folder_is_found(store, git_repo):
    inside = git_repo / "wt-in"
    run_git(git_repo, "worktree", "add", "-q", "-b", "wt", str(inside))
    session("sess-wt", inside).write()
    run_git(git_repo, "worktree", "remove", str(inside))
    [s] = find(store, Config()).sessions
    assert s.folder == "wt-in" and s.worktree == str(inside)


def test_not_found_removed_outside_worktree_nested_repository_or_similar_name(
        store, git_repo, tmp_path):
    outside = tmp_path / "wt-gone"
    run_git(git_repo, "worktree", "add", "-q", "-b", "wt", str(outside))
    session("sess-gone", outside).write("-gone")
    run_git(git_repo, "worktree", "remove", str(outside))
    nested = git_repo / "nested"
    nested.mkdir()
    run_git(nested, "init", "-q")
    session("sess-nested", nested).write("-nested")
    similar = tmp_path / "repo-other"
    similar.mkdir()
    run_git(similar, "init", "-q")
    session("sess-similar", similar).write("-similar")
    session("sess-here", git_repo).write()
    assert ids(find(store, Config())) == ["sess-here"]


# --- held (§ 4) ---

def test_a_live_session_with_a_local_record_is_not_offered(store, git_repo):
    session("sess-live", git_repo).write()
    write_session(store, SessionRecord(session_id="sess-live", author="t"), Config())
    assert ids(find(store, Config())) == []


def test_a_session_only_on_the_history_tip_is_not_offered(store, git_repo):
    session("sess-old", git_repo).write()
    path = write_session(store, SessionRecord(session_id="sess-old", author="t"), Config())
    commit_paths(store, [path], "old")
    path.unlink()  # pruned locally, or never materialized
    assert ids(find(store, Config())) == []


def test_after_sync_the_second_clone_is_not_offered_what_the_first_imported(shared_remote):
    _, a, b = shared_remote
    on = Config(sync=SyncConfig(remote="origin", fetch_throttle_seconds=0))
    sa, sb = StorePaths.resolve(a), StorePaths.resolve(b)
    script = session("sess-shared", a)
    path = script.write()
    assert take_all(sa, on).sessions == ["sess-shared"]
    assert sync.push(sa, on)
    script.cwd = str(b)
    script.lines = [x | {"cwd": str(b)} for x in script.lines]
    script.write(path=path)
    assert ids(find(sb, on)) == ["sess-shared"]
    assert sync.fetch(sb, on, force=True)
    assert ids(find(sb, on)) == []


# --- in use, unreadable ---

def test_a_transcript_in_use_is_counted_not_offered(store, git_repo):
    session("sess-busy", git_repo).write(age_days=0)
    offer = find(store, Config())
    assert offer.sessions == [] and offer.in_use == 1


def test_an_unsupported_version_is_listed_unreadable_without_a_number(store, git_repo):
    path = session("sess-future", git_repo, version="9.0.0").write()
    offer = find(store, Config())
    assert offer.items == [] and offer.sessions == []
    assert [(u.date, u.path) for u in offer.unreadable] == [("2026-01-01", path)]


# --- the record (D6) ---

def test_record_fields_come_from_the_transcript(store, git_repo, tmp_path):
    sha, out = git_commit(git_repo, "sub.txt", "from the subagent")
    folder = "-rec"
    s = Script("sess-rec", git_repo, branch="feat/x")
    s.prompt("first prompt").reply("ok", usage={"input_tokens": 10, "output_tokens": 3})
    s.tool("Edit", {"file_path": str(git_repo / "src" / "app.py")}, "edited")
    s.tool("Edit", {"file_path": str(tmp_path / "elsewhere.py")}, "edited")
    s.tool("Write", {"file_path": str(git_repo / "a.txt")}, "written")
    s.bash("ctx why a.txt:1", "session ...")
    s.tool("Task", {"description": "commit"}, "done", toolUseResult={"agentId": "ag1"})
    s.prompt("second prompt").reply("done", usage={"input_tokens": 5, "output_tokens": 2})
    main = s.write(folder)
    sub = Script("sess-rec", git_repo, agent="ag1", start="2026-01-01T10:05:30+00:00")
    sub.prompt("commit it").bash("git commit -am 'from the subagent'", out)
    sub_path = sub.write(path=main.parent / "sess-rec" / "subagents" / "agent-ag1.jsonl")
    offer = find(store, Config())
    assert offer.sessions[0].prompts == 2 and offer.sessions[0].title == "first prompt"
    assert offer.sessions[0].commits == [(sha, "from the subagent")]
    summary = run(store, Config(), offer, offer.items, [])
    assert summary.sessions == ["sess-rec"] and summary.commits == [sha] and not summary.errors
    rec = read_session(store, "sess-rec")
    assert rec.author == "t@example.com" and rec.branch == "feat/x"
    assert rec.worktree == str(git_repo)
    assert rec.started_at == "2026-01-01T10:00:00Z" and rec.ended_at == s.lines[-1]["timestamp"]
    assert [t.index for t in rec.turns] == [0, 1]
    assert rec.turns[0].tool_calls == {"edit": 3, "shell": 1, "agent": 1}
    assert rec.tokens == {"input": 15, "output": 5}
    assert rec.subagents == ["ag1"]
    assert rec.tool_calls == {"edit": 3, "ctx": 1, "agent": 1, "shell": 1}
    assert rec.ctx_calls == {"why": 1}
    assert rec.files_touched == ["a.txt", "src/app.py"]
    assert rec.imported_at and rec.ended_by is None and rec.head_start is None
    assert rec.head_end is None and rec.retrieval is None and rec.brief_tokens is None
    assert rec.transcript_bytes["source"] == main.stat().st_size + sub_path.stat().st_size
    assert rec.transcript_bytes["stored"] > 0
    copy = store.raw / "subagents" / "sess-rec" / "ag1.jsonl"
    assert copy.exists()
    [raw] = (store.raw / "sessions").glob("*-sess-rec.jsonl")
    assert raw.name == "2026-01-01-sess-rec.jsonl"
    on_tip = tip_paths(store)
    assert {"records/sessions/sess-rec.json", f"raw/sessions/{raw.name}",
            "raw/subagents/sess-rec/ag1.jsonl", f"records/checkpoints/{sha}.json"} <= on_tip
    cp = read_checkpoint(store, sha)
    assert cp.session_ids == ["sess-rec"] and cp.author == "t@example.com"
    assert cp.created_at == run_git(git_repo, "log", "-1", "--format=%cI", sha).strip()
    assert (cp.files_changed, cp.insertions, cp.deletions, cp.rewritten_from) == (1, 1, 0, None)


# --- links (§ 5) ---

def test_amend_links_the_new_commit(store, git_repo):
    old, out = git_commit(git_repo, "b.txt", "first")
    new, amended = git_commit(git_repo, "b.txt", "first, amended", "--amend")
    assert old != new and amended.startswith("[main ")
    session("sess-amend", git_repo).bash("git commit -m first", out) \
        .bash("git commit --amend -m 'first, amended'", amended).write()
    summary = take_all(store)
    assert new in summary.commits
    assert read_checkpoint(store, new).session_ids == ["sess-amend"]


def test_root_commit_and_detached_head_summary_lines_link(store, tmp_path):
    repo = tmp_path / "fresh"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "t@example.com")
    run_git(repo, "config", "user.name", "Test")
    root, out = git_commit(repo, "a.txt", "root")
    assert "(root-commit)" in out
    run_git(repo, "checkout", "-q", "--detach")
    detached, out2 = git_commit(repo, "a.txt", "detached")
    assert out2.startswith("[detached HEAD ")
    session("sess-fresh", repo).bash("git commit -m root", out).bash("git commit -m d", out2).write()
    fresh = StorePaths.resolve(repo)
    assert sorted(take_all(fresh).commits) == sorted([root, detached])


def test_a_hex_token_elsewhere_does_not_link(store, git_repo):
    sha, out = git_commit(git_repo, "b.txt", "add b")
    session("sess-hex", git_repo) \
        .bash("git commit -m x", f"nothing to commit; HEAD is {sha[:10]}\n {sha} [main]") \
        .bash("cat notes.txt", out).write()
    offer = find(store, Config())
    assert offer.sessions[0].commits == []
    assert run(store, Config(), offer, offer.items, []).commits == []
    assert not (store.records / "checkpoints").exists()


def _ambiguous_pair(repo: Path) -> str:
    """Write two commit objects whose SHAs share their first 7 hex digits; returns the prefix."""
    tree = run_git(repo, "rev-parse", "HEAD^{tree}").strip()
    seen: dict[str, bytes] = {}
    i = 0
    while True:
        body = (f"tree {tree}\nauthor T <t@example.com> 1700000000 +0000\n"
                f"committer T <t@example.com> 1700000000 +0000\n\nm{i}\n").encode()
        prefix = hashlib.sha1(b"commit %d\0" % len(body) + body).hexdigest()[:7]
        if prefix in seen:
            for data in (seen[prefix], body):
                subprocess.run(["git", "hash-object", "-w", "-t", "commit", "--stdin"], cwd=repo,
                               input=data, check=True, capture_output=True)
            return prefix
        seen[prefix] = body
        i += 1


def test_an_ambiguous_prefix_does_not_link(store, git_repo):
    prefix = _ambiguous_pair(git_repo)
    session("sess-amb", git_repo).bash("git commit -m x", f"[main {prefix}] x\n").write()
    offer = find(store, Config())
    assert offer.sessions[0].commits == []
    assert run(store, Config(), offer, offer.items, []).commits == []


def test_a_checkpointed_or_trailered_commit_is_untouched(store, git_repo):
    done, out1 = git_commit(git_repo, "b.txt", "has a checkpoint")
    path = write_checkpoint(store, CheckpointRecord(sha=done, author="x@example.com",
                                                    session_ids=["sess-live"]), Config())
    before = path.read_bytes()
    trailered, out2 = git_commit(git_repo, "c.txt", "has a trailer\n\nAI-Session: sess-other")
    session("sess-late", git_repo, commits=(out1, out2)).write()
    offer = find(store, Config())
    assert offer.sessions[0].commits == []
    assert run(store, Config(), offer, offer.items, []).commits == []
    assert path.read_bytes() == before
    assert not (store.records / "checkpoints" / f"{trailered}.json").exists()


def test_the_earlier_output_wins_and_taking_only_the_later_links_it_there(store, git_repo):
    sha, out = git_commit(git_repo, "b.txt", "shared")
    # the earlier namer has the larger ID: time decides, not the ID
    session("sess-b", git_repo, commits=(out,), start="2026-01-01T10:00:00+00:00").write("-b")
    session("sess-a", git_repo, commits=(out,), start="2026-01-02T10:00:00+00:00").write("-a")
    offer = find(store, Config())
    planned = {s.transcript.session_id: s.commits for s in offer.sessions}
    assert planned == {"sess-b": [(sha, "shared")], "sess-a": []}
    later = next(s for s in offer.sessions if s.transcript.session_id == "sess-a")
    summary = run(store, Config(), offer, [later], [])
    assert summary.commits == [sha]
    assert read_checkpoint(store, sha).session_ids == ["sess-a"]
    # the earlier one, imported later, does not take the link away
    assert take_all(store).commits == []
    assert read_checkpoint(store, sha).session_ids == ["sess-a"]


def test_a_held_session_that_named_the_commit_first_keeps_it_unlinked(store, git_repo):
    sha, out = git_commit(git_repo, "b.txt", "shared")
    session("sess-live", git_repo, commits=(out,), start="2026-01-01T10:00:00+00:00").write("-l")
    write_session(store, SessionRecord(session_id="sess-live", author="t"), Config())
    session("sess-fork", git_repo, commits=(out,), start="2026-01-02T10:00:00+00:00").write("-f")
    offer = find(store, Config())
    assert ids(offer) == ["sess-fork"] and offer.sessions[0].commits == []
    assert run(store, Config(), offer, offer.items, []).commits == []


def test_unlinked_counts_before_and_after(store, git_repo):
    _, out = git_commit(git_repo, "b.txt", "linked by import")
    git_commit(git_repo, "c.txt", "by hand")
    session("sess-one", git_repo, commits=(out,)).write()
    offer = find(store, Config())
    assert (offer.unlinked_now, offer.unlinked_after) == (3, 2)
    run(store, Config(), offer, offer.items, [])
    offer = find(store, Config())
    assert (offer.unlinked_now, offer.unlinked_after) == (2, 2)


# --- redaction ---

def test_secrets_and_account_ids_are_redacted_everywhere(store, git_repo):
    s = Script("sess-leak", git_repo)
    s.add("session-owner", accountUuid=ACCT_ID, organizationUuid=ORG_ID)
    s.prompt(f"use {SECRET} for account {ACCT_ID}").reply("ok")
    s.write()
    offer = find(store, Config())
    title = offer.sessions[0].title
    assert SECRET not in title and ACCT_ID not in title
    assert "[REDACTED:" in title and "[REDACTED:account-id]" in title
    run(store, Config(), offer, offer.items, [])
    texts = {str(p): p.read_bytes() for p in store.local.rglob("*") if p.is_file()}
    for rel, (_, sha, _) in ls_blobs(store, read_tip(store)).items():
        texts["branch:" + rel] = subprocess.run(["git", "cat-file", "blob", sha], cwd=git_repo,
                                                capture_output=True).stdout
    [raw] = [v for k, v in texts.items() if "raw/sessions" in k and k.startswith("/")]
    assert b"[REDACTED:account-id]" in raw and b"[REDACTED:" in raw
    for name, data in texts.items():
        for secret in (SECRET, ACCT_ID, ORG_ID):
            assert secret.encode() not in data.lower() and secret.encode() not in data, name


# --- atomic (§ 9) ---

def _one_of_each_on_tip(store, sid):
    paths = tip_paths(store)
    assert [p for p in paths if p.startswith("records/sessions/")] == [f"records/sessions/{sid}.json"]
    assert len([p for p in paths if p.startswith("raw/sessions/")]) == 1
    assert len(list((store.raw / "sessions").glob(f"*-{sid}.jsonl"))) == 1


def test_a_failure_after_the_raw_copy_leaves_no_record_and_the_rerun_imports_once(
        store, git_repo, monkeypatch):
    sha, out = git_commit(git_repo, "b.txt", "work")
    session("sess-int", git_repo, commits=(out,)).write()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(importer, "write_session", boom)
    summary = take_all(store)
    monkeypatch.setattr(importer, "write_session", write_session)
    assert summary.sessions == [] and "disk full" in summary.errors["sess-int"]
    assert list((store.raw / "sessions").glob("*-sess-int.jsonl"))
    assert not (store.records / "sessions" / "sess-int.json").exists()
    assert read_tip(store) is None
    offer = find(store, Config())
    assert ids(offer) == ["sess-int"] and offer.sessions[0].commits == [(sha, "work")]
    assert run(store, Config(), offer, offer.items, []).sessions == ["sess-int"]
    _one_of_each_on_tip(store, "sess-int")
    assert f"records/checkpoints/{sha}.json" in tip_paths(store)


def test_a_failure_before_the_ref_commit_is_offered_again_and_committed(
        store, git_repo, monkeypatch):
    sha, out = git_commit(git_repo, "b.txt", "work")
    session("sess-int", git_repo, commits=(out,)).write()

    def boom(*a, **k):
        raise OSError("ref locked")

    monkeypatch.setattr(importer, "commit_paths", boom)
    assert "ref locked" in take_all(store).errors["sess-int"]
    monkeypatch.setattr(importer, "commit_paths", commit_paths)
    assert read_session(store, "sess-int").imported_at
    assert read_checkpoint(store, sha).session_ids == ["sess-int"]
    offer = find(store, Config())
    assert ids(offer) == ["sess-int"] and offer.sessions[0].commits == [(sha, "work")]
    summary = run(store, Config(), offer, offer.items, [])
    assert summary.sessions == ["sess-int"] and summary.commits == [sha]
    _one_of_each_on_tip(store, "sess-int")
    assert f"records/checkpoints/{sha}.json" in tip_paths(store)
    assert ids(find(store, Config())) == []


# --- concurrent (§ 9) ---

def test_import_runs_beside_git_hooks_and_live_capture(store, git_repo, tmp_path):
    outputs = {}
    for n in range(3):
        sha, out = git_commit(git_repo, f"imp{n}.txt", f"import {n}")
        outputs[sha] = out
        session(f"sess-imp{n}", git_repo, commits=(out,),
                start=f"2026-01-0{n + 1}T10:00:00+00:00").write(f"-imp{n}")
    offer = find(store, Config())
    assert len(offer.sessions) == 3
    live = session("sess-live", git_repo).write(path=tmp_path / "live" / "sess-live.jsonl")
    errors: list[BaseException] = []
    results = {}

    def importing():
        try:
            results["summary"] = run(store, Config(), offer, offer.items, [])
        except BaseException as e:
            errors.append(e)

    def hooks():
        try:
            for n in range(3):
                sha, _ = git_commit(git_repo, f"live{n}.txt", f"live {n}\n\nAI-Session: sess-live")
                results.setdefault("live", []).append(sha)
                githooks_worker.run("checkpoint", str(git_repo), sha)
            worker.run("final", "sess-live", str(live), str(git_repo))
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=importing), threading.Thread(target=hooks)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors and not any(t.is_alive() for t in threads)
    assert results["summary"].errors == {}
    paths = tip_paths(store)
    for sid in ["sess-imp0", "sess-imp1", "sess-imp2", "sess-live"]:
        assert f"records/sessions/{sid}.json" in paths, sid
    for sha in [*outputs, *results["live"]]:
        assert f"records/checkpoints/{sha}.json" in paths, sha
    assert read_checkpoint(store, results["live"][0]).session_ids == ["sess-live"]
    assert sorted(results["summary"].commits) == sorted(outputs)


# --- documents ---

def _docs(git_repo: Path) -> None:
    files = {"docs/adr.md": "# Decisions\n\nWe chose the quokka protocol.\n",
             "notes/Design-notes.MD": "# Notes\n\ntext\n", "README.md": "# Readme\n",
             "CLAUDE.md": "rules\n", "src/CLAUDE.md": "more rules\n", "other.txt": "no\n"}
    for path, text in files.items():
        (git_repo / path).parent.mkdir(parents=True, exist_ok=True)
        (git_repo / path).write_text(text)
    run_git(git_repo, "add", ".")
    run_git(git_repo, "commit", "-q", "-m", "docs")


def test_documents_taken_declined_and_listed_again_on_request(store, git_repo):
    _docs(git_repo)
    offer = find(store, load_config(git_repo))
    assert [(d.path, d.group) for d in offer.documents] == [
        ("docs/adr.md", "suggested"), ("notes/Design-notes.MD", "suggested"),
        ("README.md", "other")]
    adr, _, readme = offer.documents
    summary = run(store, Config(), offer, [adr], [readme])
    assert summary.documents == ["docs/adr.md"] and summary.declined == ["document:README.md"]
    assert load_config(git_repo).documents.paths == ("docs/adr.md",)
    conn = connect(store, readonly=True)
    try:
        [hit] = search(conn, "quokka", kind="document")
    finally:
        conn.close()
    assert hit.ref == "docs/adr.md:1"
    offer = find(store, load_config(git_repo))
    assert [d.path for d in offer.documents] == ["notes/Design-notes.MD"]
    assert offer.declined == []
    offer = find(store, load_config(git_repo), include_declined=True)
    assert [i.key for i in offer.items] == ["document:notes/Design-notes.MD", "document:README.md"]
    run(store, Config(), offer, [offer.declined[0]], [])
    data = json.loads((store.state / "import.json").read_text())
    assert "declined_documents" not in data
    assert load_config(git_repo).documents.paths == ("docs/adr.md", "README.md")


def test_declined_sessions_are_remembered_and_taking_one_clears_it(store, git_repo):
    session("sess-no", git_repo).write()
    offer = find(store, Config())
    summary = run(store, Config(), offer, [], offer.sessions)
    assert summary.sessions == [] and summary.declined == ["session:sess-no"]
    assert find(store, Config()).items == []
    offer = find(store, Config(), include_declined=True)
    assert [i.key for i in offer.items] == ["session:sess-no"]
    assert run(store, Config(), offer, offer.declined, []).sessions == ["sess-no"]
    assert json.loads((store.state / "import.json").read_text()) == {}


def test_an_item_both_taken_and_declined_is_refused(store, git_repo):
    session("sess-x", git_repo).write()
    offer = find(store, Config())
    with pytest.raises(ValueError):
        run(store, Config(), offer, offer.items, offer.items)
    assert read_tip(store) is None


# --- later runs, token, retention, forget ---

def test_a_second_run_offers_nothing(store, git_repo):
    _docs(git_repo)
    _, out = git_commit(git_repo, "b.txt", "work")
    session("sess-1", git_repo, commits=(out,)).write("-1")
    session("sess-2", git_repo).write("-2")
    summary = take_all(store)
    assert sorted(summary.sessions) == ["sess-1", "sess-2"] and len(summary.documents) == 3
    offer = find(store, load_config(git_repo))
    assert offer.items == [] and offer.unreadable == [] and offer.in_use == 0


def test_a_session_going_idle_after_the_preview_changes_the_token(store, git_repo):
    session("sess-old", git_repo).write("-old")
    path = session("sess-busy", git_repo).write("-busy", age_days=0)
    first = find(store, Config())
    assert ids(first) == ["sess-old"] and len(first.token) == 8
    assert find(store, Config()).token == first.token
    stamp = time.time() - 86400
    os.utime(path, (stamp, stamp))
    again = find(store, Config())
    assert len(again.sessions) == 2 and again.token != first.token


def test_an_old_imported_session_is_pruned_like_a_live_one(store, git_repo):
    session("sess-old", git_repo, start="2026-01-01T10:00:00+00:00").write()
    take_all(store)
    [raw] = (store.raw / "sessions").glob("*-sess-old.jsonl")
    config = dataclasses.replace(Config(), retention=RetentionConfig(raw_days=30))
    started = datetime.fromisoformat(read_session(store, "sess-old").started_at)
    assert datetime.now(started.tzinfo) - started > timedelta(days=30)
    out = worker.sweep(store, config)
    assert out["pruned"] == [f"raw/sessions/{raw.name}"] and not raw.exists()


def test_a_forgotten_imported_session_is_offered_again_with_a_hint(store, git_repo, monkeypatch,
                                                                    capsys):
    from wscontext.cli.main import main
    session("sess-gone", git_repo).write()
    take_all(store)
    monkeypatch.chdir(git_repo)
    capsys.readouterr()
    assert main(["forget", "sess-gone"]) == 0
    assert "`ctx import` offers it again" in capsys.readouterr().out
    assert not json.loads((store.state / "import.json").read_text()).get("declined_sessions")
    assert ids(find(store, Config())) == ["sess-gone"]


def test_forgetting_a_live_session_declines_nothing(store, git_repo, monkeypatch, capsys):
    from wscontext.cli.main import main
    path = write_session(store, SessionRecord(session_id="sess-live", author="t"), Config())
    commit_paths(store, [path], "live")
    monkeypatch.chdir(git_repo)
    capsys.readouterr()
    assert main(["forget", "sess-live"]) == 0
    assert not (store.state / "import.json").exists()
    # no transcript in Claude Code's folder: no hint
    assert "ctx import" not in capsys.readouterr().out


def test_the_history_ref_is_the_only_ref_the_import_moves(store, git_repo):
    _, out = git_commit(git_repo, "b.txt", "work")
    session("sess-refs", git_repo, commits=(out,)).write()
    before = run_git(git_repo, "for-each-ref", "--format=%(refname) %(objectname)")
    take_all(store)
    after = run_git(git_repo, "for-each-ref", "--format=%(refname) %(objectname)")
    moved = set(after.splitlines()) - set(before.splitlines())
    assert [line.split()[0] for line in moved] == [HISTORY_REF]
