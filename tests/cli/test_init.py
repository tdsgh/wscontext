import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from cli_support import isolated_git, spawned  # noqa: F401
from conftest import run_git

from wscontext.cli import init as ctx_init
from wscontext.cli.main import main
from wscontext.cli.sync import set_remote
from wscontext.config import Config, load_config
from wscontext.githooks.install import HOOKS
from wscontext.history.branch import HISTORY_REF, LEGACY_REF, commit_paths, read_tip
from wscontext.store.paths import StorePaths

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def ctx(capsys, *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out.removesuffix("\n"), out.err.removesuffix("\n")


@pytest.fixture
def plugin(tmp_path: Path) -> dict[str, Path]:
    """A plugin root with a launcher that runs the Git-hook dispatcher, and an empty data dir."""
    root = tmp_path / "cache" / "wscontext" / "0.1.0"
    (root / "bin").mkdir(parents=True)
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin" / "plugin.json").write_text('{"name": "wscontext", "version": "0.1.0"}')
    launcher = root / "bin" / "ctx"
    launcher.write_text('#!/bin/sh\nwhile [ "${1#--plugin}" != "$1" ]; do shift 2; done\n'
                        '[ "$1" = git-hook ] && shift\n'
                        f"exec '{sys.executable}' -m wscontext.githooks.dispatch \"$@\"\n")
    launcher.chmod(0o755)
    data = tmp_path / "data"
    data.mkdir()
    (data / "plugin-root").write_text(str(root))
    (data / "venv").mkdir()
    (data / "venv" / ".lock-hash").write_text(f"{root} 1 2")
    return {"root": root, "data": data}


@pytest.fixture
def opts(plugin) -> list[str]:
    return ["--plugin-root", str(plugin["root"]), "--plugin-data", str(plugin["data"])]


@pytest.fixture
def repo(git_repo: Path, monkeypatch, spawned) -> Path:  # noqa: F811
    monkeypatch.chdir(git_repo)
    return git_repo


def test_default_config_template_is_the_defaults():
    text = ctx_init.default_config_toml()
    parsed = tomllib.loads(text)
    assert set(parsed) == {"redaction", "retention", "sync", "hooks", "capture", "logs",
                           "retrieval", "store", "graph"}
    assert "remote" not in parsed["sync"]
    # the committed file and the template agree with the dataclasses, and so with each other
    assert tomllib.loads((REPO_ROOT / ".wscontext" / "config.toml").read_text()) == parsed


def test_init_doctor_uninstall_roundtrip(repo, opts, plugin, capsys):
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.write_text("#!/bin/sh\necho mine >> .mine\n")
    hook.chmod(0o755)
    (repo / "CLAUDE.md").write_text("# Mine\n\nKeep this.\n")

    code, out, _ = ctx(capsys, *opts, "init")
    assert code == 0, out
    assert "created .wscontext/config.toml" in out
    assert "existing hooks kept and chained: post-commit" in out
    assert load_config(repo) == Config()
    for name in HOOKS:
        assert (repo / ".git" / "hooks" / name).exists()
    stable = plugin["data"] / "bin" / "ctx"
    assert str(stable) in (repo / ".git" / "hooks" / "pre-push").read_text()
    claude_md = (repo / "CLAUDE.md").read_text()
    assert claude_md.startswith("# Mine\n\nKeep this.\n") and "`ctx why" in claude_md

    # the chained hook and wscontext both run on a commit
    (repo / "b.txt").write_text("b\n")
    run_git(repo, "add", "b.txt")
    run_git(repo, "commit", "-q", "-m", "second")
    assert (repo / ".mine").exists()

    code, out, _ = ctx(capsys, *opts, "doctor")
    assert "[  OK] git hooks: 7 of 7 installed" in out
    assert "chained hooks: post-commit" in out
    assert "[  OK] git hook launcher" in out
    assert "[FAIL]" not in out

    code, out, _ = ctx(capsys, *opts, "uninstall")
    assert code == 0, out
    assert "post-commit: restored" in out and "CLAUDE.md block removed" in out
    assert hook.read_text() == "#!/bin/sh\necho mine >> .mine\n"
    assert not (repo / ".git" / "hooks" / "pre-push").exists()
    assert (repo / "CLAUDE.md").read_text() == "# Mine\n\nKeep this.\n"
    assert (repo / ".wscontext" / "config.toml").exists()
    assert StorePaths.resolve(repo).local.exists()


def test_init_is_idempotent(repo, opts, capsys):
    ctx(capsys, *opts, "init")
    first = (repo / "CLAUDE.md").read_text(), (repo / ".git" / "hooks" / "commit-msg").read_text()
    config = (repo / ".wscontext" / "config.toml").read_text()
    code, out, _ = ctx(capsys, *opts, "init")
    assert code == 0
    assert "kept .wscontext/config.toml" in out and "CLAUDE.md block already present" in out
    assert ((repo / "CLAUDE.md").read_text(), (repo / ".git" / "hooks" / "commit-msg").read_text()) == first
    assert (repo / ".wscontext" / "config.toml").read_text() == config
    assert (repo / "CLAUDE.md").read_text().count(ctx_init.BLOCK_BEGIN) == 1
    assert not list((repo / ".git" / "hooks").glob("*.pre-wscontext"))


def test_uninstall_deletes_a_claude_md_it_created(repo, opts, capsys):
    ctx(capsys, *opts, "init")
    ctx(capsys, "uninstall")
    assert not (repo / "CLAUDE.md").exists()


def test_block_replacement_keeps_surrounding_text():
    text = f"top\n\n{ctx_init.BLOCK_BEGIN}\nold\n{ctx_init.BLOCK_END}\n\nbottom\n"
    updated = ctx_init.set_block(text)
    assert "old" not in updated and updated.startswith("top\n") and updated.endswith("\nbottom\n")
    assert ctx_init.remove_block(updated) == "top\n\nbottom\n"


def test_init_without_a_launcher_is_an_error(repo, capsys):
    code, _, err = ctx(capsys, "init")
    assert code == 1 and "plugin launcher" in err
    assert not (repo / ".wscontext").exists()


def test_init_launcher_option_for_development(repo, tmp_path, capsys):
    launcher = tmp_path / "dev-ctx"
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    code, _, _ = ctx(capsys, "init", "--launcher", str(launcher))
    assert code == 0
    assert str(launcher) in (repo / ".git" / "hooks" / "post-commit").read_text()


def test_init_reports_hooks_it_cannot_manage(repo, opts, capsys):
    shared = repo.parent / "shared-hooks"
    shared.mkdir()
    run_git(repo, "config", "core.hooksPath", str(shared))
    code, out, _ = ctx(capsys, *opts, "init")
    assert code == 0 and "not managed by wscontext" in out and "pre-push:" in out
    assert not any(shared.iterdir())
    _, out, _ = ctx(capsys, *opts, "doctor")
    assert "[WARN] git hooks" in out


def test_init_reports_a_conflicting_parked_original(repo, opts, capsys):
    hooks = repo / ".git" / "hooks"
    (hooks / "post-commit").write_text("#!/bin/sh\n")
    (hooks / "post-commit.pre-wscontext").write_text("#!/bin/sh\n")
    code, out, _ = ctx(capsys, *opts, "init")
    assert code == 1 and "an original is already parked" in out and "post-commit" in out


def test_stable_launcher_forwards_to_the_recorded_plugin_root(tmp_path):
    data = tmp_path / "data"
    root = tmp_path / "root"
    (root / "bin").mkdir(parents=True)
    (data / "bin").mkdir(parents=True)
    (root / "bin" / "ctx").write_text('#!/bin/sh\nprintf "%s|" "$@"\n')
    (root / "bin" / "ctx").chmod(0o755)
    (data / "plugin-root").write_text(str(root))
    stable = ctx_init.write_stable_launcher(data)
    out = subprocess.run([str(stable), "git-hook", "post-commit"], capture_output=True, text=True,
                         cwd=tmp_path)
    assert out.stdout == f"--plugin-data|{data}|git-hook|post-commit|"
    # an update moves the plugin root; the stable launcher follows without being rewritten
    new = tmp_path / "root2"
    (new / "bin").mkdir(parents=True)
    (new / "bin" / "ctx").write_text('#!/bin/sh\necho new\n')
    (new / "bin" / "ctx").chmod(0o755)
    (data / "plugin-root").write_text(str(new))
    assert subprocess.run([str(stable)], capture_output=True, text=True).stdout == "new\n"


def test_stable_launcher_text_names_the_cmd_launcher_on_windows():
    assert "bin/ctx.cmd" in ctx_init.stable_launcher_text("win32")
    assert "bin/ctx.cmd" not in ctx_init.stable_launcher_text("linux")


def test_doctor_flags_a_launcher_inside_the_plugin_cache(repo, plugin, capsys):
    cache = repo.parent / "home" / ".claude" / "plugins" / "cache" / "wscontext" / "wscontext" / "0.1.0"
    (cache / "bin").mkdir(parents=True)
    (cache / "bin" / "ctx").write_text("#!/bin/sh\n")
    (cache / "bin" / "ctx").chmod(0o755)
    ctx(capsys, "init", "--launcher", str(cache / "bin" / "ctx"))
    code, out, _ = ctx(capsys, "doctor")
    assert code == 1 and "[FAIL] git hook launcher" in out and "versioned plugin cache" in out


def test_doctor_detects_a_stale_launcher(repo, opts, plugin, capsys):
    ctx(capsys, *opts, "init")
    hook = repo / ".git" / "hooks" / "pre-push"
    stable = str(plugin["data"] / "bin" / "ctx")
    cache = "/home/u/.claude/plugins/cache/wscontext/wscontext/0.1.0/bin/ctx"
    hook.write_text(hook.read_text().replace(stable, cache))
    code, out, _ = ctx(capsys, *opts, "doctor")
    assert code == 1
    assert "versioned plugin cache" in out or "does not exist" in out
    assert "fix: run `ctx init`" in out
    # init rewrites the shim to the stable launcher
    ctx(capsys, *opts, "init")
    code, out, _ = ctx(capsys, *opts, "doctor")
    assert code == 0, out


def test_doctor_detects_a_launcher_outside_the_stable_path(repo, opts, tmp_path, capsys):
    other = tmp_path / "elsewhere" / "ctx"
    other.parent.mkdir()
    other.write_text("#!/bin/sh\n")
    other.chmod(0o755)
    ctx(capsys, "init", "--launcher", str(other))
    code, out, _ = ctx(capsys, *opts, "doctor")
    assert code == 1 and "is not this plugin's stable launcher" in out


def test_doctor_detects_a_plugin_root_that_is_gone(repo, opts, plugin, capsys):
    ctx(capsys, *opts, "init")
    (plugin["data"] / "plugin-root").write_text(str(plugin["root"].parent / "0.0.9"))
    code, out, _ = ctx(capsys, *opts, "doctor")
    assert code == 1 and "names a plugin root that is gone" in out


def test_doctor_before_init_fails_with_the_fix(repo, opts, capsys):
    code, out, _ = ctx(capsys, *opts, "doctor")
    assert code == 1
    assert "[FAIL] repository" in out and "[FAIL] git hooks: not installed" in out
    assert "fix: run `ctx init` in this repository" in out


def test_doctor_json_and_claude_hook_health(repo, opts, capsys):
    from wscontext.config import Config
    from wscontext.logs import HOOKS_LOG, append_log, now_iso
    ctx(capsys, *opts, "init")
    store = StorePaths.resolve(repo)
    _, out, _ = ctx(capsys, *opts, "--json", "doctor")
    checks = {c["check"]: c for c in json.loads(out)["checks"]}
    assert checks["claude code hooks"]["status"] == "warn"
    assert "fix" in checks["claude code hooks"]
    append_log(store, Config(), HOOKS_LOG, {"ts": now_iso(), "event": "SessionStart", "ok": True})
    _, out, _ = ctx(capsys, *opts, "--json", "doctor")
    checks = {c["check"]: c for c in json.loads(out)["checks"]}
    assert checks["claude code hooks"]["status"] == "ok"


def test_doctor_checks_the_plugin_environment(repo, opts, plugin, capsys):
    _, out, _ = ctx(capsys, *opts, "doctor")
    assert "plugin: version 0.1.0" in out and "[  OK] environment" in out
    (plugin["data"] / "venv" / ".lock-hash").write_text("/old/root 1 2")
    _, out, _ = ctx(capsys, *opts, "doctor")
    assert "[FAIL] environment" in out


def test_set_remote_edits_the_sync_section_only():
    text = ctx_init.default_config_toml()
    for before in (text, text.replace("[sync]\n", '[sync]\nremote = "old"\n')):
        updated = set_remote(before, "origin")
        assert tomllib.loads(updated)["sync"]["remote"] == "origin"
        assert tomllib.loads(updated)["retention"] == tomllib.loads(text)["retention"]
        assert updated.count("remote =") == 1
    assert tomllib.loads(set_remote("", "origin")) == {"sync": {"remote": "origin"}}


def test_sync_setup_validates_the_remote_name(repo, capsys):
    code, _, err = ctx(capsys, "sync", "--setup")
    assert code == 1 and "name the remote with --remote" in err
    code, _, err = ctx(capsys, "sync", "--setup", "--remote", "git@host:me/repo.git")
    assert code == 1 and "a remote name, not a URL" in err
    code, _, err = ctx(capsys, "sync", "--setup", "--remote", "nope")
    assert code == 1 and "not a configured Git remote" in err
    assert not (repo / ".wscontext" / "config.toml").exists()


def test_sync_is_off_until_setup(repo, capsys):
    for flags in ([], ["--status"], ["--dry-run"]):
        code, _, err = ctx(capsys, "sync", *flags)
        assert code == 1 and "sharing is off" in err


def test_sync_setup_status_dry_run_and_push(shared_remote, monkeypatch, spawned, capsys):
    bare, a, b = shared_remote
    monkeypatch.chdir(a)
    code, out, _ = ctx(capsys, "sync", "--setup")  # one remote: origin
    assert code == 0 and "sync remote set to origin" in out
    assert load_config(a).sync.remote == "origin"
    assert load_config(a) != Config()

    from cli_support import add_session
    store = StorePaths.resolve(a)
    paths = add_session(store, "sess-one")
    commit_paths(store, paths, "session sess-one")
    code, out, _ = ctx(capsys, "sync", "--dry-run")
    assert code == 0 and "sessions a push would send: 1: sess-one" in out
    # nothing pushed, in either layout
    assert run_git(bare, "for-each-ref", "refs/wscontext/history",
                   "refs/heads/wscontext/history").strip() == ""

    code, out, _ = ctx(capsys, "sync")
    assert code == 0 and "pushed history to origin" in out
    assert run_git(bare, "rev-parse", "--verify", "wscontext/history").strip()
    _, out, _ = ctx(capsys, "sync", "--status")
    assert "0 ahead, 0 behind" in out and "sessions a push would send: 0" in out

    # a second machine initializes and recovers the shared history
    monkeypatch.chdir(b)
    (b / ".wscontext").mkdir()
    (b / ".wscontext" / "config.toml").write_text(set_remote(ctx_init.default_config_toml(), "origin"))
    launcher = b.parent / "ctx"
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    code, out, _ = ctx(capsys, "init", "--launcher", str(launcher))
    assert code == 0 and "history fetched from origin and materialized" in out
    assert (StorePaths.resolve(b).records / "sessions" / "sess-one.json").exists()


def test_sync_push_with_no_history_pushes_nothing(repo, capsys, tmp_path):
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    run_git(repo, "remote", "add", "origin", str(bare))
    ctx(capsys, "sync", "--setup")
    code, out, _ = ctx(capsys, "sync")
    assert code == 0 and "nothing to push" in out


def test_block_per_retrieval_switch():
    on, off = ctx_init.claude_block(True), ctx_init.claude_block(False)
    assert "ctx find" in on and "ctx why" in on
    assert "ctx find" not in off and "ctx why" in off
    assert on.startswith(ctx_init.BLOCK_BEGIN) and off.endswith(ctx_init.BLOCK_END + "\n")
    # a run replaces the block in place, in both directions, and keeps the text around it
    text = f"top\n\n{on}\nbottom\n"
    assert ctx_init.set_block(text, False) == f"top\n\n{off}\nbottom\n"
    assert ctx_init.set_block(ctx_init.set_block(text, False), True) == text


def test_init_writes_the_block_for_the_switch(repo, opts, capsys):
    ctx(capsys, *opts, "init")
    assert "ctx find" in (repo / "CLAUDE.md").read_text()
    config = repo / ".wscontext" / "config.toml"
    config.write_text(config.read_text().replace("enabled = true", "enabled = false"))
    code, out, _ = ctx(capsys, *opts, "init")
    assert code == 0 and "CLAUDE.md block updated" in out
    text = (repo / "CLAUDE.md").read_text()
    assert "ctx find" not in text and text.count(ctx_init.BLOCK_BEGIN) == 1


def _doctor_line(out: str, name: str) -> str:
    return next(line for line in out.splitlines() if f"] {name}:" in line)


def test_doctor_new_items_on_a_fresh_repository(repo, opts, capsys):
    ctx(capsys, *opts, "init")
    _, out, _ = ctx(capsys, *opts, "doctor")
    assert "[FAIL]" not in out
    assert "[  OK] search engine: SQLite" in _doctor_line(out, "search engine")
    assert "[  OK] search index: not built yet" in _doctor_line(out, "search index")
    assert f"[  OK] history ref: {HISTORY_REF} (no history yet)" in _doctor_line(out, "history ref")
    assert "[  OK] claude.md block: matches the retrieval switch (on)" in _doctor_line(out, "claude.md block")
    assert "[  OK] size limits: no session or store over its limit" in _doctor_line(out, "size limits")
    assert "tracking refs" not in out


def test_doctor_flags_a_block_that_does_not_match_the_switch(repo, opts, capsys):
    ctx(capsys, *opts, "init")
    config = repo / ".wscontext" / "config.toml"
    config.write_text(config.read_text().replace("enabled = true", "enabled = false"))
    _, out, _ = ctx(capsys, *opts, "doctor")
    line = _doctor_line(out, "claude.md block")
    assert line.startswith("[WARN]") and "has the `ctx find` trigger, but retrieval is off" in line
    assert "fix: run `ctx init` in this repository" in out
    assert "not used: [retrieval] enabled = false" in _doctor_line(out, "search index")
    ctx(capsys, *opts, "init")
    _, out, _ = ctx(capsys, *opts, "doctor")
    assert "(off, no `ctx find` trigger)" in _doctor_line(out, "claude.md block")
    # a stage-1 block lacks the trigger while retrieval is on
    config.write_text(config.read_text().replace("enabled = false", "enabled = true"))
    _, out, _ = ctx(capsys, *opts, "doctor")
    assert "lacks the `ctx find` trigger" in _doctor_line(out, "claude.md block")


def test_doctor_index_not_built_yet_while_sessions_have_no_copy(repo, opts, capsys):
    from cli_support import add_session
    ctx(capsys, *opts, "init")
    add_session(StorePaths.resolve(repo), "sess-one", transcript=False)
    _, out, _ = ctx(capsys, *opts, "doctor")
    assert "[  OK] search index: not built yet" in _doctor_line(out, "search index")


def test_doctor_warns_on_missing_index_after_raw_copies_were_pruned(repo, opts, capsys):
    from cli_support import add_session
    from wscontext.store.records import read_session, write_session
    ctx(capsys, *opts, "init")
    store = StorePaths.resolve(repo)
    add_session(store, "sess-one", transcript=False)
    record = read_session(store, "sess-one")
    record.transcript_bytes = {"stored": 10}
    write_session(store, record, Config())
    _, out, _ = ctx(capsys, *opts, "doctor")
    assert "[WARN] search index: missing, although sessions are captured" in out
    assert not list((store.raw / "sessions").glob("*"))


def test_doctor_reports_index_lag_and_size_warnings(repo, opts, capsys):
    from cli_support import add_session
    ctx(capsys, *opts, "init")
    store = StorePaths.resolve(repo)
    add_session(store, "sess-one")
    _, out, _ = ctx(capsys, *opts, "doctor")
    assert "[WARN] search index: missing, although sessions are captured" in out
    ctx(capsys, "rebuild")
    _, out, _ = ctx(capsys, *opts, "doctor")
    assert "[  OK] search index: 1 sessions" in out and "lag 0 sources" in out
    config = repo / ".wscontext" / "config.toml"
    config.write_text(config.read_text().replace("session_warn_bytes = 10485760", "session_warn_bytes = 100"))
    _, out, _ = ctx(capsys, *opts, "doctor")
    line = _doctor_line(out, "size limits")
    assert line.startswith("[WARN]") and "session sess-one" in line and "is over the 100 B limit" in line


def test_doctor_stale_tracking_refs_and_the_history_move(shared_remote, opts, monkeypatch, spawned, capsys):
    bare, a, _ = shared_remote
    monkeypatch.chdir(a)
    ctx(capsys, *opts, "init")
    ctx(capsys, "sync", "--setup")
    from cli_support import add_session
    store = StorePaths.resolve(a)
    commit_paths(store, add_session(store, "sess-one"), "session sess-one")
    tip = read_tip(store)

    # tracking refs of a remote that was removed
    run_git(a, "update-ref", "refs/wscontext/remotes/gone/history", tip)
    run_git(a, "update-ref", "refs/wscontext/remotes/gone/legacy-history", tip)
    _, out, _ = ctx(capsys, "doctor")
    line = _doctor_line(out, "tracking refs")
    assert line.startswith("[WARN]") and "remote 'gone', which no longer exists" in line
    assert "fix: git update-ref -d refs/wscontext/remotes/gone/history && " \
           "git update-ref -d refs/wscontext/remotes/gone/legacy-history" in out
    assert "origin" not in line
    for ref in ("history", "legacy-history"):
        run_git(a, "update-ref", "-d", f"refs/wscontext/remotes/gone/{ref}")

    # a local stage-1 branch
    run_git(a, "update-ref", LEGACY_REF, tip)
    _, out, _ = ctx(capsys, "doctor")
    assert f"[WARN] history ref: local stage-1 branch {LEGACY_REF} still exists" in out
    run_git(a, "update-ref", "-d", LEGACY_REF)

    # the remote still has the stage-1 branch: doctor and `ctx sync --status` say so, the push removes it
    run_git(a, "push", "-q", "origin", f"{tip}:{LEGACY_REF}")
    ctx(capsys, "sync", "--setup")  # fetches
    _, out, _ = ctx(capsys, "doctor")
    assert f"[WARN] history ref: origin still has the stage-1 branch {LEGACY_REF}" in out
    _, out, _ = ctx(capsys, "sync", "--status")
    assert f"origin still has the stage-1 history branch {LEGACY_REF}" in out
    code, out, _ = ctx(capsys, "sync")
    assert code == 0 and "pushed history to origin" in out
    assert f"the stage-1 history branch {LEGACY_REF} was removed from origin" in out
    assert run_git(bare, "for-each-ref", LEGACY_REF).strip() == ""
    _, out, _ = ctx(capsys, "doctor")
    assert f"[  OK] history ref: {HISTORY_REF}" in out


def test_block_triggers_for_the_graph_follow_the_switch():
    on, off = ctx_init.claude_block(True), ctx_init.claude_block(False)
    assert "`ctx code <name>`" in on and "`ctx impact <name>`" in on
    assert "ctx code" not in off and "ctx impact" not in off


def test_doctor_flags_a_block_without_the_graph_triggers(repo, opts, capsys):
    ctx(capsys, *opts, "init")
    claude = repo / "CLAUDE.md"
    claude.write_text(claude.read_text().replace(ctx_init.GRAPH_TRIGGERS, ""))
    _, out, _ = ctx(capsys, *opts, "doctor")
    line = _doctor_line(out, "claude.md block")
    assert line.startswith("[WARN]") and "lacks the `ctx code` and `ctx impact` triggers" in line
    ctx(capsys, *opts, "init")
    _, out, _ = ctx(capsys, *opts, "doctor")
    assert _doctor_line(out, "claude.md block").startswith("[  OK]")


def test_doctor_reports_the_parser_languages_and_graph(repo, opts, capsys):
    ctx(capsys, *opts, "init")
    _, out, _ = ctx(capsys, *opts, "doctor")
    assert "[  OK] code parser: tree-sitter" in _doctor_line(out, "code parser")
    for name in ("python", "typescript", "javascript"):
        assert _doctor_line(out, f"language {name}").endswith(": on")
    assert "not built yet" in _doctor_line(out, "code graph")
    config = repo / ".wscontext" / "config.toml"
    config.write_text(config.read_text().replace('"javascript"]', '"cobol"]'))
    _, out, _ = ctx(capsys, *opts, "doctor")
    assert _doctor_line(out, "language javascript").endswith(": off")
    assert "unknown" in _doctor_line(out, "language cobol")
    (repo / "a.py").write_text("def f():\n    pass\n")
    run_git(repo, "add", "a.py")
    ctx(capsys, *opts, "rebuild", "--graph")
    (repo / "a.py").write_text("def f():\n    return 1\n\n\ndef g():\n    pass\n")
    _, out, _ = ctx(capsys, *opts, "doctor")
    line = _doctor_line(out, "code graph")
    assert "1 files" in line and "last refresh 0 min ago" in line and "1 stale file" in line


def test_doctor_reports_a_language_whose_grammar_is_unavailable(repo, opts, capsys, monkeypatch):
    from wscontext.cli import doctor
    ctx(capsys, *opts, "init")
    monkeypatch.setattr(doctor, "load_parts", lambda enabled: ([], ["typescript"]))
    _, out, _ = ctx(capsys, *opts, "doctor")
    line = _doctor_line(out, "language typescript")
    assert line.startswith("[WARN]") and "grammar is unavailable" in line
