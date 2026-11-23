import json
from pathlib import Path

from wscontext.adapters import claude_home
from wscontext.adapters.claude_home import config_dir as real_config_dir


def _projects(home: Path) -> Path:
    return claude_home.projects_dir()


def _put(folder: Path, name: str, lines: list[dict]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def test_fixture_keeps_real_folder_out(no_real_claude_home):
    assert claude_home.config_dir() == no_real_claude_home
    assert Path.home() / ".claude" not in claude_home.projects_dir().parents


def test_undo_falls_back_to_the_guard(monkeypatch, never_real_claude_files):
    from wscontext.redact import account
    monkeypatch.undo()  # also undoes the per-test fixtures
    assert claude_home.config_dir() == never_real_claude_files / "claude-home"
    assert account.account_file_path() == never_real_claude_files / ".claude.json"


def test_config_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "moved"))
    assert real_config_dir() == tmp_path / "moved"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert real_config_dir() == Path.home() / ".claude"


def test_transcripts_cwd_on_line_three(no_real_claude_home):
    _put(_projects(no_real_claude_home) / "p1", "abc.jsonl", [
        {"type": "queue-operation", "timestamp": "T0"},
        {"type": "user", "sessionId": "abc", "gitBranch": "dev", "version": "2.0.5",
         "timestamp": "T1"},
        {"type": "user", "cwd": "/w/repo", "timestamp": "T2"},
        {"type": "user", "cwd": "/other"},
    ])
    [t] = claude_home.transcripts()
    assert (t.session_id, t.cwd, t.branch, t.version, t.first_ts) == (
        "abc", "/w/repo", "dev", "2.0.5", "T0")
    assert t.mtime > 0


def test_transcripts_skips(no_real_claude_home):
    folder = _projects(no_real_claude_home) / "p1"
    _put(folder, "nocwd.jsonl", [{"type": "user", "sessionId": "nocwd"}])
    _put(folder, "stem.jsonl", [{"sessionId": "other", "cwd": "/w"}])
    _put(folder, ".hidden.jsonl", [{"sessionId": ".hidden", "cwd": "/w"}])
    _put(folder, "ok.jsonl", [{"sessionId": "ok", "cwd": "/w"}])
    _put(folder / "ok" / "subagents", "agent-1.jsonl", [{"sessionId": "agent-1", "cwd": "/w"}])
    (folder / "ok.txt").write_text("x")
    (folder / "broken.jsonl").write_bytes(b"\xff\xfe not json\n")
    assert [t.session_id for t in claude_home.transcripts()] == ["ok"]


def test_projects_missing(no_real_claude_home):
    assert claude_home.transcripts() == []


def _settings(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cleanupPeriodDays": value}))
    return path


def test_cleanup_days(tmp_path, no_real_claude_home):
    repo = tmp_path / "repo"
    assert claude_home.cleanup_days(repo) == (30, None)
    user = _settings(no_real_claude_home / "settings.json", 90)
    assert claude_home.cleanup_days(repo) == (90, user)
    project = _settings(repo / ".claude" / "settings.json", 60)
    assert claude_home.cleanup_days(repo) == (60, project)
    local = _settings(repo / ".claude" / "settings.local.json", 7)
    assert claude_home.cleanup_days(repo) == (7, local)


def test_cleanup_days_ignores_bad_values(tmp_path, no_real_claude_home):
    repo = tmp_path / "repo"
    user = _settings(no_real_claude_home / "settings.json", 45)
    _settings(repo / ".claude" / "settings.local.json", "ten")
    _settings(repo / ".claude" / "settings.json", True)
    assert claude_home.cleanup_days(repo) == (45, user)
    (repo / ".claude" / "settings.json").write_text("{broken")
    assert claude_home.cleanup_days(repo) == (45, user)
