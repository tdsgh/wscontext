import os
import subprocess
import sys
import time

from githook_support import (activate, git_run, head, isolated_git, launcher, log_lines,  # noqa: F401
                             pty_run, repo, wait_log, write_hook)

from conftest import run_git

from wscontext.githooks.install import install
from wscontext.history.branch import HISTORY_REF, read_tip
from wscontext.store.paths import StorePaths


def _with_remote(repo, tmp_path, sync_remote: str | None = "origin"):
    bare = tmp_path / "remote.git"
    run_git(tmp_path, "init", "-q", "--bare", str(bare))
    run_git(repo, "remote", "add", "origin", str(bare))
    if sync_remote:
        (repo / ".wscontext").mkdir(exist_ok=True)
        (repo / ".wscontext" / "config.toml").write_text(f'[sync]\nremote = "{sync_remote}"\n')
    return bare


def _linked_commit(repo, store) -> str:
    activate(store, "s-1")
    (repo / "b.txt").write_text("b\n")
    run_git(repo, "add", "b.txt")
    run_git(repo, "commit", "-q", "-m", "linked")
    wait_log(store, "worker:git-checkpoint")
    return read_tip(store)


def _remote_ref(bare, ref: str) -> str | None:
    out = run_git(bare, "for-each-ref", "--format=%(objectname)", ref).strip()
    return out or None


def test_pre_push_shares_history_with_the_users_push(repo, tmp_path):
    bare = _with_remote(repo, tmp_path)
    store = StorePaths.resolve(repo)
    tip = _linked_commit(repo, store)
    pushed = git_run(repo, "push", "-q", "origin", "main")
    assert pushed.returncode == 0, pushed.stderr
    assert _remote_ref(bare, "refs/heads/main") == head(repo)
    assert wait_log(store, "worker:git-push")[0]["ok"] is True
    assert _remote_ref(bare, HISTORY_REF) == tip


def test_nothing_is_shared_without_a_sync_remote(repo, tmp_path):
    bare = _with_remote(repo, tmp_path, sync_remote=None)
    store = StorePaths.resolve(repo)
    _linked_commit(repo, store)
    assert git_run(repo, "push", "-q", "origin", "main").returncode == 0
    time.sleep(1)  # a spawned worker would have logged by now
    assert log_lines(store, "git:pre-push") and not log_lines(store, "worker:git-push")
    assert _remote_ref(bare, HISTORY_REF) is None


def test_failing_original_pre_push_fails_the_push_and_shares_nothing(git_repo, launcher, tmp_path):
    write_hook(git_repo / ".git" / "hooks" / "pre-push",
               'cat > "$(git rev-parse --git-dir)/push-input"; exit 4\n')
    install(git_repo, launcher)
    bare = _with_remote(git_repo, tmp_path)
    store = StorePaths.resolve(git_repo)
    _linked_commit(git_repo, store)
    failed = git_run(git_repo, "push", "-q", "origin", "main")
    assert failed.returncode != 0
    zero = "0" * 40
    assert (git_repo / ".git" / "push-input").read_text() == \
        f"refs/heads/main {head(git_repo)} refs/heads/main {zero}\n"
    time.sleep(1)
    assert not log_lines(store, "git:pre-push")
    assert _remote_ref(bare, "refs/heads/main") is None and _remote_ref(bare, HISTORY_REF) is None


def test_wscontext_failure_never_fails_a_push(repo, launcher, tmp_path):
    bare = _with_remote(repo, tmp_path)
    store = StorePaths.resolve(repo)
    _linked_commit(repo, store)
    launcher.write_text("#!/bin/sh\ncat >/dev/null; exit 9\n")
    assert git_run(repo, "push", "-q", "origin", "main").returncode == 0
    (repo / "c.txt").write_text("c\n")
    run_git(repo, "add", "c.txt")
    run_git(repo, "commit", "-q", "-m", "more")
    # A sync remote that does not exist: the history push fails, the user's push does not.
    (repo / ".wscontext" / "config.toml").write_text('[sync]\nremote = "nowhere"\n')
    launcher.write_text(f"#!/bin/sh\nshift\nexec '{sys.executable}' -m wscontext.githooks.dispatch"
                        ' "$@"\n')
    launcher.chmod(0o755)
    assert git_run(repo, "push", "-q", "origin", "main").returncode == 0
    assert _remote_ref(bare, "refs/heads/main") == head(repo)
    assert wait_log(store, "worker:git-push")[0]["ok"] is False


def test_pre_push_never_waits_on_a_credential_prompt(repo, tmp_path, monkeypatch):
    """The history push may need credentials the user's push did not; it must not prompt.

    The user's push runs on a real terminal. The sync remote's transport asks Git for
    credentials, which prompts on that terminal when it can. The history push runs in
    a detached worker with no terminal, so the prompt fails at once instead of blocking.
    """
    bare = _with_remote(repo, tmp_path, sync_remote="history")
    # A budget long enough that a prompt inside the hook itself would show in the timing.
    (repo / ".wscontext" / "config.toml").write_text(
        '[sync]\nremote = "history"\n[hooks]\ntime_budget_ms = 20000\n')
    run_git(repo, "remote", "add", "history", "wsctest::needs-credentials")
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    write_hook(helpers / "git-remote-wsctest",
               'printf "protocol=https\\nhost=example.invalid\\n\\n" | git credential fill'
               ' >/dev/null; exit 1\n')
    monkeypatch.setenv("PATH", f"{helpers}{os.pathsep}{os.environ['PATH']}")
    store = StorePaths.resolve(repo)
    _linked_commit(repo, store)
    started = time.monotonic()
    try:
        code = pty_run(tmp_path, repo, "git", "push", "-q", "origin", "main", timeout=60)
    except subprocess.TimeoutExpired:
        raise AssertionError("the user's push blocked on a credential prompt") from None
    assert code == 0 and time.monotonic() - started < 10
    assert _remote_ref(bare, "refs/heads/main") == head(repo)
    assert wait_log(store, "worker:git-push", timeout=30)[0]["ok"] is False


def test_git_hook_path_imports_only_stdlib():
    code = ("import sys\nbefore = set(sys.modules)\n"
            "import wscontext.githooks.dispatch, wscontext.githooks.worker, "
            "wscontext.githooks.install\n"
            "new = {m.split('.')[0] for m in set(sys.modules) - before}\n"
            "print(sorted(new - set(sys.stdlib_module_names) - {'wscontext'}))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]", out.stdout
