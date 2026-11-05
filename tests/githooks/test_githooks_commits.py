import os
import sys
import time

from githook_support import (activate, checkpoint, deactivate, git_run, head, isolated_git,  # noqa: F401
                             launcher, log_lines, repo, trailers, wait_log, write_hook)

from conftest import run_git

from wscontext.capture.state import read_json, active_path, turn_state_path, worktree_key
from wscontext.config import load_config
from wscontext.githooks import dispatch, handlers
from wscontext.githooks.install import install
from wscontext.history.branch import HISTORY_REF, read_tip
from wscontext.store.paths import StorePaths


def _commit(repo, name: str, text: str = "x\n", *extra: str) -> str:
    (repo / name).write_text(text)
    run_git(repo, "add", name)
    run_git(repo, "commit", "-q", "-m", f"add {name}", *extra)
    return head(repo)


def _on_branch(repo, sha: str) -> bool:
    return git_run(repo, "cat-file", "-e", f"{HISTORY_REF}:records/checkpoints/{sha}.json") \
        .returncode == 0


def test_commit_with_active_session_gets_trailer_and_checkpoint(repo):
    store = StorePaths.resolve(repo)
    activate(store, "s-1")
    sha = _commit(repo, "b.txt", "one\ntwo\n")
    assert trailers(repo) == ["s-1"]
    wait_log(store, "worker:git-checkpoint")
    rec = checkpoint(store, sha)
    assert rec.session_ids == ["s-1"] and rec.author == "t@example.com"
    assert (rec.files_changed, rec.insertions, rec.deletions) == (1, 2, 0)
    assert rec.rewritten_from is None and _on_branch(repo, sha)
    assert head(repo) == sha and run_git(repo, "status", "--porcelain") == ""
    assert all(e["ok"] for e in log_lines(store, "git:post-commit"))


def test_redacted_worktree_path_still_finds_the_active_session(repo):
    # a rule matching the worktree path redacts the active.json key; lookups must follow
    (repo / ".wscontext").mkdir()
    (repo / ".wscontext" / "config.toml").write_text('[redaction]\nextra_patterns = ["repo$"]\n')
    store = StorePaths.resolve(repo)
    config = load_config(repo)
    activate(store, "s-1", config=config)
    key = worktree_key(store, config)
    assert key.endswith("[REDACTED:custom-1]") and str(store.root) not in read_json(active_path(store))
    assert list(read_json(active_path(store))[key]) == ["s-1"]
    sha = _commit(repo, "b.txt")
    assert trailers(repo) == ["s-1"]
    wait_log(store, "worker:git-checkpoint")
    assert checkpoint(store, sha).session_ids == ["s-1"]
    deactivate(store, "s-1", config)
    assert read_json(active_path(store)) == {key: {}}


def test_commit_with_no_session_gets_neither(repo):
    store = StorePaths.resolve(repo)
    sha = _commit(repo, "b.txt")
    time.sleep(1)  # a spawned worker would have logged by now
    assert trailers(repo) == [] and checkpoint(store, sha) is None
    assert log_lines(store, "git:post-commit") and not log_lines(store, "worker:git-checkpoint")
    assert read_tip(store) is None


def test_stale_session_is_not_linked_until_it_is_active_again(repo):
    store = StorePaths.resolve(repo)
    activate(store, "s-old", minutes_ago=180)
    _commit(repo, "b.txt")
    assert trailers(repo) == []
    turn = turn_state_path(store, "s-old")
    turn.parent.mkdir(parents=True, exist_ok=True)
    turn.write_text("{}\n")  # a prompt or tool call just now
    _commit(repo, "c.txt")
    assert trailers(repo) == ["s-old"]


def test_several_sessions_each_get_a_trailer_once(repo):
    store = StorePaths.resolve(repo)
    activate(store, "s-a", minutes_ago=5)
    activate(store, "s-b")
    _commit(repo, "b.txt")
    assert trailers(repo) == ["s-a", "s-b"]
    run_git(repo, "commit", "-q", "--amend", "--no-edit")
    assert trailers(repo) == ["s-a", "s-b"]


def test_editor_commit_is_linked_and_an_untouched_editor_still_aborts(repo, tmp_path):
    store = StorePaths.resolve(repo)
    activate(store, "s-ed")
    editor = write_hook(tmp_path / "editor", 'printf "typed subject\\n" | cat - "$1" > "$1.new"'
                                             ' && mv "$1.new" "$1"\n')
    (repo / "b.txt").write_text("b\n")
    run_git(repo, "add", "b.txt")
    before = head(repo)
    aborted = git_run(repo, "commit", env={"GIT_EDITOR": "true"})
    assert aborted.returncode != 0 and head(repo) == before
    done = git_run(repo, "commit", env={"GIT_EDITOR": str(editor)})
    assert done.returncode == 0, done.stderr
    assert git_run(repo, "log", "-1", "--format=%s").stdout.strip() == "typed subject"
    assert trailers(repo) == ["s-ed"]


def test_session_in_another_worktree_does_not_link(repo, tmp_path):
    other = tmp_path / "other"
    run_git(repo, "worktree", "add", "-q", str(other), "-b", "other")
    activate(StorePaths.resolve(other), "s-other")
    _commit(repo, "b.txt")
    assert trailers(repo) == []
    _commit(other, "c.txt")
    assert trailers(other) == ["s-other"]


def test_amend_writes_a_new_checkpoint_and_keeps_the_old(repo):
    store = StorePaths.resolve(repo)
    activate(store, "s-1")
    old = _commit(repo, "b.txt")
    wait_log(store, "worker:git-checkpoint")
    run_git(repo, "commit", "-q", "--amend", "-m", "reworded")
    new = head(repo)
    wait_log(store, "worker:git-rewrite")
    wait_log(store, "worker:git-checkpoint", 2)
    assert checkpoint(store, old).rewritten_from is None
    rec = checkpoint(store, new)
    assert rec.session_ids == ["s-1"] and rec.rewritten_from == old
    assert _on_branch(repo, old) and _on_branch(repo, new)


def test_rebase_writes_new_checkpoints_without_adding_the_current_session(repo):
    store = StorePaths.resolve(repo)
    run_git(repo, "checkout", "-q", "-b", "feature")
    activate(store, "s-1")
    picked = [_commit(repo, "f1.txt"), _commit(repo, "f2.txt")]
    wait_log(store, "worker:git-checkpoint", 2)
    deactivate(store, "s-1")
    activate(store, "s-2")
    run_git(repo, "checkout", "-q", "main")
    _commit(repo, "m.txt")
    run_git(repo, "checkout", "-q", "feature")
    run_git(repo, "rebase", "-q", "main")
    wait_log(store, "worker:git-rewrite")
    rebased = run_git(repo, "rev-list", "--reverse", "main..feature").split()
    assert len(rebased) == 2 and not set(rebased) & set(picked)
    for old, new in zip(picked, rebased):
        assert trailers(repo, new) == ["s-1"]
        rec = checkpoint(store, new)
        assert rec.session_ids == ["s-1"] and rec.rewritten_from == old
        assert _on_branch(repo, new)


def test_existing_hook_runs_first_and_its_failure_still_fails_the_commit(git_repo, launcher):
    hooks = git_repo / ".git" / "hooks"
    write_hook(hooks / "prepare-commit-msg",
               'echo "$@" >> "$(git rev-parse --git-dir)/original-ran"\n'
               '[ -f "$(git rev-parse --git-dir)/fail" ] && exit 3\nexit 0\n')
    install(git_repo, launcher)
    store = StorePaths.resolve(git_repo)
    activate(store, "s-1")
    sha = _commit(git_repo, "b.txt")
    ran = (git_repo / ".git" / "original-ran").read_text()
    assert ran.startswith(".git/COMMIT_EDITMSG message") and trailers(git_repo) == ["s-1"]
    (git_repo / ".git" / "fail").write_text("")
    (git_repo / "c.txt").write_text("c\n")
    run_git(git_repo, "add", "c.txt")
    failed = git_run(git_repo, "commit", "-q", "-m", "blocked")
    assert failed.returncode != 0 and head(git_repo) == sha


def test_chained_post_rewrite_gets_the_same_input(git_repo, launcher):
    hooks = git_repo / ".git" / "hooks"
    write_hook(hooks / "post-rewrite", 'cat > "$(git rev-parse --git-dir)/rewrite-input"\n')
    install(git_repo, launcher)
    store = StorePaths.resolve(git_repo)
    activate(store, "s-1")
    old = _commit(git_repo, "b.txt")
    run_git(git_repo, "commit", "-q", "--amend", "-m", "again")
    assert (git_repo / ".git" / "rewrite-input").read_text() == f"{old} {head(git_repo)}\n"
    wait_log(store, "worker:git-rewrite")
    assert checkpoint(store, head(git_repo)).rewritten_from == old


def test_wscontext_failure_never_fails_a_commit(repo, launcher):
    store = StorePaths.resolve(repo)
    activate(store, "s-1")
    launcher.write_text("#!/bin/sh\nexit 9\n")
    _commit(repo, "b.txt")
    assert trailers(repo) == []
    launcher.unlink()
    _commit(repo, "c.txt")
    launcher.write_text(f"#!/bin/sh\nshift\nexec '{sys.executable}' -m wscontext.githooks.dispatch"
                        ' "$@"\n')
    launcher.chmod(0o755)
    # A store that cannot be written: every handler fails inside, the commit does not.
    (repo / ".git" / "wscontext" / "state" / "active.json").unlink()
    (repo / ".git" / "wscontext" / "state" / "active.json").mkdir()
    sha = _commit(repo, "d.txt")
    assert head(repo) == sha and trailers(repo) == []


def test_handler_errors_and_bad_input_exit_zero(repo, monkeypatch):
    monkeypatch.chdir(repo)
    for argv in ([], ["nope"], ["prepare-commit-msg"], ["prepare-commit-msg", "/no/such/file"],
                 ["post-commit", "extra"]):
        assert dispatch.main(argv) == 0
    store = StorePaths.resolve(repo)
    activate(store, "s-1")
    entry = dispatch.run("prepare-commit-msg", [str(repo / "missing")], "", cwd=repo)
    assert entry["ok"] is False and "FileNotFoundError" in entry["error"]
    assert "unknown git hook" in dispatch.run("nope", [], "", cwd=repo)["error"]
    assert log_lines(store, "git:nope")


def test_slow_handler_stops_at_time_budget(repo, monkeypatch):
    (repo / ".wscontext").mkdir()
    (repo / ".wscontext" / "config.toml").write_text("[hooks]\ntime_budget_ms = 100\n")
    monkeypatch.setitem(handlers.HANDLERS, "post-commit", lambda *a: time.sleep(3))
    started = time.monotonic()
    entry = dispatch.run("post-commit", [], "", cwd=repo)
    assert time.monotonic() - started < 2
    assert entry["ok"] is False and "timeout" in entry["error"]


def test_scissors_and_comments_count_as_empty():
    assert handlers._is_empty("\n# Please enter\n#\n", ("#",))
    assert handlers._is_empty("# ------------------------ >8 ------------------------\n"
                              "diff --git a/x b/x\n", ("#",))
    assert not handlers._is_empty("subject\n# comment\n", ("#",))
    assert not handlers._is_empty("; not a comment here\n", ("#",))
    assert handlers._is_empty("; comment\n", tuple(handlers.AUTO_COMMENT_CHARS))


def test_real_hook_environment_in_a_linked_worktree(git_repo, launcher, tmp_path):
    """Git sets GIT_DIR and GIT_INDEX_FILE (here an index.lock) for the hooks and the worker."""
    write_hook(git_repo / ".git" / "hooks" / "prepare-commit-msg",
               'env | grep "^GIT_" > "$(git rev-parse --git-common-dir)/hook-env"\n')
    install(git_repo, launcher)
    wt = tmp_path / "wt"
    run_git(git_repo, "worktree", "add", "-q", str(wt), "-b", "side")
    main_head = head(git_repo)
    store = StorePaths.resolve(wt)
    assert store.local == StorePaths.resolve(git_repo).local
    activate(store, "s-wt")
    (wt / "a.txt").write_text("changed\n")
    run_git(wt, "commit", "-q", "-a", "-m", "from the worktree")
    sha = head(wt)
    env = dict(line.split("=", 1) for line in
               (git_repo / ".git" / "hook-env").read_text().splitlines())
    assert env["GIT_DIR"].endswith(os.path.join(".git", "worktrees", "wt"))
    assert env["GIT_INDEX_FILE"].endswith("index.lock")
    wait_log(store, "worker:git-checkpoint")
    assert trailers(wt) == ["s-wt"] and checkpoint(store, sha).session_ids == ["s-wt"]
    assert _on_branch(git_repo, sha)
    assert head(wt) == sha and head(git_repo) == main_head
    for tree in (wt, git_repo):
        assert run_git(tree, "status", "--porcelain") == ""
        assert run_git(tree, "diff", "--cached", "--name-only") == ""


def _diverge(repo, active: str | None = None) -> None:
    """`main` and `feature` each carry one commit the other lacks."""
    run_git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "f.txt")
    run_git(repo, "checkout", "-q", "main")
    _commit(repo, "m.txt")
    if active:
        activate(StorePaths.resolve(repo), active)


def test_merge_commit_with_active_session_gets_a_checkpoint(repo):
    store = StorePaths.resolve(repo)
    _diverge(repo, "s-1")
    run_git(repo, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    sha = head(repo)
    assert trailers(repo) == ["s-1"]
    wait_log(store, "worker:git-checkpoint")
    rec = checkpoint(store, sha)
    assert rec.session_ids == ["s-1"] and rec.rewritten_from is None and _on_branch(repo, sha)
    assert all(e["ok"] for e in log_lines(store, "git:post-merge"))


def test_fast_forward_merge_writes_no_checkpoint(repo):
    store = StorePaths.resolve(repo)
    run_git(repo, "checkout", "-q", "-b", "feature")
    theirs = _commit(repo, "f.txt", "x\n", "--trailer", "AI-Session: s-theirs")
    wait_log(store, "worker:git-checkpoint")  # their commit's own checkpoint
    run_git(repo, "checkout", "-q", "main")
    activate(store, "s-1")
    run_git(repo, "merge", "-q", "feature")
    assert head(repo) == theirs
    time.sleep(1)
    assert log_lines(store, "git:post-merge") and len(log_lines(store, "worker:git-checkpoint")) == 1


def test_fast_forward_onto_a_merge_commit_writes_no_checkpoint(repo):
    store = StorePaths.resolve(repo)
    run_git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "s.txt")
    run_git(repo, "checkout", "-q", "-b", "feature", "main")
    # someone else's merge commit whose first parent is our HEAD
    run_git(repo, "merge", "-q", "--no-ff", "-m", "their merge\n\nAI-Session: s-theirs", "side")
    theirs = head(repo)
    wait_log(store, "worker:git-checkpoint")  # their merge's own checkpoint
    run_git(repo, "checkout", "-q", "main")
    activate(store, "s-1")
    run_git(repo, "merge", "-q", "feature")
    assert head(repo) == theirs and trailers(repo) == ["s-theirs"]
    time.sleep(1)
    assert len(log_lines(store, "git:post-merge")) == 2
    assert len(log_lines(store, "worker:git-checkpoint")) == 1


def test_squash_merge_writes_no_checkpoint_until_the_commit(repo):
    store = StorePaths.resolve(repo)
    _diverge(repo, "s-1")
    run_git(repo, "merge", "-q", "--squash", "feature")
    time.sleep(1)
    assert log_lines(store, "git:post-merge") and not log_lines(store, "worker:git-checkpoint")
    run_git(repo, "commit", "-q", "-m", "squashed")
    wait_log(store, "worker:git-checkpoint")
    assert checkpoint(store, head(repo)).session_ids == ["s-1"]


def test_merge_with_no_session_writes_no_checkpoint(repo):
    store = StorePaths.resolve(repo)
    _diverge(repo)
    run_git(repo, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    time.sleep(1)
    assert trailers(repo) == [] and checkpoint(store, head(repo)) is None
    assert log_lines(store, "git:post-merge") and not log_lines(store, "worker:git-checkpoint")
