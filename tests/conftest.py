import subprocess
from pathlib import Path

import pytest


def run_git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def put_store_file(local: Path, rel: str, data: bytes) -> Path:
    """Write a file under a local store (`StorePaths.local`) at a branch-style path."""
    path = local.joinpath(*rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def branch_blob(repo: Path, rev: str, rel: str) -> bytes:
    return subprocess.run(["git", "cat-file", "blob", f"{rev}:{rel}"], cwd=repo, check=True,
                          capture_output=True).stdout


def set_identity(repo: Path) -> None:
    run_git(repo, "config", "user.email", "t@example.com")
    run_git(repo, "config", "user.name", "Test")
    run_git(repo, "config", "commit.gpgsign", "false")


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    set_identity(repo)
    (repo / "a.txt").write_text("a\n")
    run_git(repo, "add", "a.txt")
    run_git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def shared_remote(tmp_path: Path, git_repo: Path) -> tuple[Path, Path, Path]:
    """A bare remote under tmp_path and two clones of it, `a` and `b`, with remote `origin`."""
    bare = tmp_path / "remote.git"
    run_git(tmp_path, "clone", "-q", "--bare", str(git_repo), str(bare))
    clones = []
    for name in ("a", "b"):
        run_git(tmp_path, "clone", "-q", str(bare), str(tmp_path / name))
        set_identity(tmp_path / name)
        clones.append(tmp_path / name)
    return bare, clones[0], clones[1]
