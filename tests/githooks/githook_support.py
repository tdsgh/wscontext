import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from wscontext.capture.state import active_path, update_json
from wscontext.githooks.install import install
from wscontext.store.paths import StorePaths
from wscontext.store.records import RecordError, read_checkpoint

# Runs a command on a fresh pseudo-terminal that becomes its controlling terminal,
# so anything that prompts on /dev/tty blocks exactly as in an interactive shell.
PTY_RUN = """\
import os, pty, sys
pid, fd = pty.fork()
if pid == 0:
    os.execvp(sys.argv[1], sys.argv[1:])
while True:
    try:
        if not os.read(fd, 1024):
            break
    except OSError:
        break
sys.exit(os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]))
"""


@pytest.fixture(autouse=True)
def isolated_git(tmp_path_factory, monkeypatch):
    """Git in these tests reads no user or system config and has no askpass program."""
    empty = tmp_path_factory.mktemp("gitconfig") / "config"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_ASKPASS", "SSH_ASKPASS", "GIT_EDITOR", "GIT_DIR", "GIT_INDEX_FILE",
                 "GIT_WORK_TREE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def launcher(tmp_path: Path) -> Path:
    """A stand-in for the plugin's `bin/ctx`: `ctx git-hook …` runs the Git-hook dispatcher."""
    path = tmp_path / "plugin" / "bin" / "ctx"
    path.parent.mkdir(parents=True)
    path.write_text(f'#!/bin/sh\n[ "$1" = git-hook ] && shift\n'
                    f"exec '{sys.executable}' -m wscontext.githooks.dispatch \"$@\"\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def repo(git_repo: Path, launcher: Path) -> Path:
    install(git_repo, launcher)
    return git_repo


def git_run(cwd: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full = None if env is None else {**os.environ, **env}
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=full,
                          timeout=60)


def pty_run(tmp_path: Path, cwd: Path, *cmd: str, timeout: float) -> int:
    script = tmp_path / "pty_run.py"
    script.write_text(PTY_RUN)
    return subprocess.run([sys.executable, str(script), *cmd], cwd=cwd, capture_output=True,
                          timeout=timeout).returncode


def activate(store: StorePaths, sid: str, minutes_ago: float = 0) -> None:
    """Mark a session active in the store's worktree, as the SessionStart hook does."""
    at = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    update_json(active_path(store), lambda d: d.setdefault(str(store.root), {}).__setitem__(sid, at))


def deactivate(store: StorePaths, sid: str) -> None:
    update_json(active_path(store), lambda d: d.get(str(store.root), {}).pop(sid, None))


def log_lines(store: StorePaths, event: str) -> list[dict]:
    try:
        text = (store.logs / "hooks.jsonl").read_text()
    except FileNotFoundError:
        return []
    return [e for e in map(json.loads, text.splitlines()) if e["event"] == event]


def wait_log(store: StorePaths, event: str, count: int = 1, timeout: float = 30) -> list[dict]:
    """Wait for a detached worker: it logs its line when it is done."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lines = log_lines(store, event)
        if len(lines) >= count:
            return lines
        time.sleep(0.05)
    raise AssertionError(f"no {count} x {event} in the hook log: {log_lines(store, event)}")


def checkpoint(store: StorePaths, sha: str):
    try:
        return read_checkpoint(store, sha)
    except (OSError, RecordError):
        return None


def trailers(repo: Path, rev: str = "HEAD") -> list[str]:
    out = git_run(repo, "log", "-1", "--format=%(trailers:key=AI-Session,valueonly)", rev).stdout
    return [line for line in out.splitlines() if line]


def head(repo: Path) -> str:
    return git_run(repo, "rev-parse", "HEAD").stdout.strip()


def write_hook(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path
