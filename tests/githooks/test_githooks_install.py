from githook_support import isolated_git, launcher, write_hook  # noqa: F401  (fixtures)

from conftest import run_git

from wscontext.githooks.install import (CHAINED_SUFFIX, HOOKS, install, manual_line, shim_text,
                                        uninstall)


def _hooks(repo):
    return repo / ".git" / "hooks"


def test_install_writes_executable_shims(git_repo, launcher):
    report = install(git_repo, launcher)
    assert report.installed == list(HOOKS) and report.chained == [] and not report.manual
    for hook in HOOKS:
        path = _hooks(git_repo) / hook
        assert path.stat().st_mode & 0o111
        assert path.read_text() == shim_text(hook, launcher, "darwin")
    assert run_git(git_repo, "status", "--porcelain") == ""


def test_existing_hook_is_chained_and_reinstall_is_idempotent(git_repo, launcher):
    original = write_hook(_hooks(git_repo) / "post-commit", "echo mine\n")
    body = original.read_bytes()
    first = install(git_repo, launcher)
    assert first.chained == ["post-commit"]
    chained = _hooks(git_repo) / ("post-commit" + CHAINED_SUFFIX)
    assert chained.read_bytes() == body
    second = install(git_repo, launcher.parent / "moved-ctx")
    assert second.chained == [] and second.installed == list(HOOKS)
    assert chained.read_bytes() == body
    assert "moved-ctx" in (_hooks(git_repo) / "post-commit").read_text()


def test_uninstall_restores_the_originals(git_repo, launcher):
    hooks = _hooks(git_repo)
    original = write_hook(hooks / "pre-push", "exit 0\n")
    body, mode = original.read_bytes(), original.stat().st_mode
    samples = sorted(p.name for p in hooks.glob("*.sample"))
    install(git_repo, launcher)
    done = uninstall(git_repo)
    assert done["pre-push"] == "restored"
    assert all(done[h] == "removed" for h in HOOKS if h != "pre-push")
    assert original.read_bytes() == body and original.stat().st_mode == mode
    assert not (hooks / ("pre-push" + CHAINED_SUFFIX)).exists()
    assert sorted(p.name for p in hooks.iterdir()) == sorted([*samples, "pre-push"])
    assert uninstall(git_repo) == {}


def test_uninstall_leaves_a_hook_that_replaced_the_shim(git_repo, launcher):
    install(git_repo, launcher)
    foreign = write_hook(_hooks(git_repo) / "post-commit", "echo someone else\n")
    assert "post-commit" not in uninstall(git_repo)
    assert foreign.read_text().endswith("someone else\n")


def test_parked_original_is_never_overwritten(git_repo, launcher):
    hooks = _hooks(git_repo)
    parked = write_hook(hooks / ("post-commit" + CHAINED_SUFFIX), "echo parked\n")
    foreign = write_hook(hooks / "post-commit", "echo newer\n")
    report = install(git_repo, launcher)
    assert report.conflicts == ["post-commit"] and "post-commit" not in report.installed
    assert parked.read_text().endswith("parked\n") and foreign.read_text().endswith("newer\n")


def test_hooks_path_in_the_working_tree_is_reported_manual(git_repo, launcher):
    husky = write_hook(git_repo / ".husky" / "pre-commit", "npx lint-staged\n")
    run_git(git_repo, "add", ".husky")
    run_git(git_repo, "commit", "-q", "-m", "husky")
    run_git(git_repo, "config", "core.hooksPath", ".husky")
    before = sorted(p.name for p in (git_repo / ".husky").iterdir())
    report = install(git_repo, launcher)
    assert report.installed == [] and set(report.manual) == set(HOOKS)
    assert report.manual["post-commit"] == manual_line("post-commit", launcher, "darwin")
    assert sorted(p.name for p in (git_repo / ".husky").iterdir()) == before
    assert husky.read_text() == "#!/bin/sh\nnpx lint-staged\n"
    assert not any((_hooks(git_repo) / h).exists() for h in HOOKS)
    assert run_git(git_repo, "status", "--porcelain") == ""
    assert uninstall(git_repo) == {}


def test_hooks_path_outside_the_repository_is_reported_manual(git_repo, launcher, tmp_path):
    shared = tmp_path / "shared-hooks"
    shared.mkdir()
    run_git(git_repo, "config", "core.hooksPath", str(shared))
    report = install(git_repo, launcher)
    assert report.installed == [] and set(report.manual) == set(HOOKS)
    assert list(shared.iterdir()) == []


def test_shim_text_per_platform():
    posix = shim_text("post-commit", "/opt/it's here/bin/ctx", "linux")
    assert posix.startswith("#!/bin/sh\n") and posix.count("\n") == 3
    assert "l='/opt/it'\\''s here/bin/ctx'" in posix
    assert 'git-hook post-commit "$@" </dev/null' in posix
    assert "$(cat)" not in posix and "@" not in posix.replace('"$@"', "")
    win = shim_text("post-commit", r"C:\Users\me\plugin\bin\ctx.cmd", "win32")
    assert "l='C:/Users/me/plugin/bin/ctx.cmd'" in win and "\\" not in win.split("\n", 2)[2]
    assert win.replace("C:/Users/me/plugin/bin/ctx.cmd", "X") == \
        shim_text("post-commit", "X", "linux")
    assert manual_line("pre-push", r"C:\p\ctx.cmd", "win32") == \
        "[ -x 'C:/p/ctx.cmd' ] && 'C:/p/ctx.cmd' git-hook pre-push \"$@\" >/dev/null 2>&1 || true"


def test_stdin_hooks_forward_the_input_and_only_pre_push_is_guarded():
    for hook in ("post-rewrite", "pre-push"):
        text = shim_text(hook, "/x/ctx", "linux")
        assert "i=$(cat)" in text and text.count('printf "%s${i:+\\n}" "$i" |') == 2
    assert "[ $r -eq 0 ] && [ -x" in shim_text("pre-push", "/x/ctx", "linux")
    assert "[ $r -eq 0 ]" not in shim_text("post-rewrite", "/x/ctx", "linux")
    assert "[ $r -eq 0 ]" not in shim_text("post-commit", "/x/ctx", "linux")
