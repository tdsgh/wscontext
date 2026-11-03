from conftest import run_git

from wscontext.store.paths import StorePaths


def test_worktrees_share_one_store(git_repo, tmp_path):
    wt = tmp_path / "wt"
    run_git(git_repo, "worktree", "add", "-q", str(wt), "-b", "other")
    main = StorePaths.resolve(git_repo)
    other = StorePaths.resolve(wt)
    assert main.local == other.local
    assert main.local == (git_repo / ".git" / "wscontext").resolve()
    assert main.root != other.root
    assert other.committed == other.root / ".wscontext"
    assert main.raw == main.local / "raw" and main.logs == main.local / "logs"


def test_directories_not_created(git_repo):
    p = StorePaths.resolve(git_repo)
    assert not p.local.exists()


def test_resolve_from_subdirectory(git_repo):
    sub = git_repo / "sub"
    sub.mkdir()
    assert StorePaths.resolve(sub).root.resolve() == git_repo.resolve()
