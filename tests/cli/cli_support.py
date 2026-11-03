import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import run_git

from wscontext.cli.main import main
from wscontext.config import Config
from wscontext.history.branch import commit_paths
from wscontext.store.paths import StorePaths
from wscontext.store.records import (CheckpointRecord, SessionRecord, TurnRecord,
                                     write_checkpoint, write_session)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
DATES = {"c1": "2026-01-01T12:00:00+00:00", "c2": "2026-01-02T12:00:00+00:00"}
APP = "def alpha():\n    return 1\n\ndef beta():\n    return 2\n"


@pytest.fixture(autouse=True)
def isolated_git(tmp_path_factory, monkeypatch):
    empty = tmp_path_factory.mktemp("gitconfig") / "config"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def commit(repo: Path, message: str, date: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True, env=env,
                   capture_output=True)
    return run_git(repo, "rev-parse", "HEAD").strip()


def add_session(store: StorePaths, sid: str, *, transcript: bool = True, files=("src/app.py",),
                branch: str = "main", author: str = "t@example.com", started="2026-01-01T10:00:00Z",
                config: Config = Config()) -> list[Path]:
    """A session record, plus the fixture transcript copied under the raw store."""
    write_session(store, SessionRecord(
        session_id=sid, author=author, branch=branch, started_at=started,
        ended_at="2026-01-01T10:05:00Z", files_touched=list(files),
        tool_calls={"read": 2, "edit": 1}, turns=[
            TurnRecord(session_id=sid, author=author, index=0, started_at="2026-01-01T10:00:00Z"),
            TurnRecord(session_id=sid, author=author, index=1, started_at="2026-01-01T10:01:00Z")]),
        config)
    paths = [store.records / "sessions" / f"{sid}.json"]
    if transcript:
        raw = store.raw / "sessions" / f"{started[:10]}-{sid}.jsonl"
        raw.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / "transcripts" / "known" / "sess1.jsonl", raw)
        paths.append(raw)
    return paths


@dataclass
class World:
    repo: Path
    store: StorePaths
    c1: str  # app.py created, linked to sess-one
    c2: str  # line 2 changed, no session

    def ctx(self, capsys, *argv: str) -> tuple[int, str, str]:
        capsys.readouterr()
        code = main(list(argv))
        out = capsys.readouterr()
        return code, out.out.removesuffix("\n"), out.err.removesuffix("\n")

    def sub(self, text: str) -> str:
        return text.replace(self.c1[:12], "{c1}").replace(self.c2[:12], "{c2}")


@pytest.fixture
def world(git_repo: Path, monkeypatch) -> World:
    """Lines 1,3-5 from c1 (session sess-one); line 2 from c2 (no session); line 5 edited, uncommitted."""
    monkeypatch.chdir(git_repo)
    store = StorePaths.resolve(git_repo)
    (git_repo / "src").mkdir()
    (git_repo / "src" / "app.py").write_text(APP)
    run_git(git_repo, "add", "src")
    c1 = commit(git_repo, "add app\n\nAI-Session: sess-one", DATES["c1"])
    (git_repo / "src" / "app.py").write_text(APP.replace("return 1", "return 11"))
    run_git(git_repo, "add", "src")
    c2 = commit(git_repo, "tweak alpha", DATES["c2"])
    (git_repo / "src" / "app.py").write_text(
        APP.replace("return 1", "return 11").replace("return 2", "return 22"))
    paths = add_session(store, "sess-one")
    commit_paths(store, paths, "session sess-one")
    return World(git_repo, store, c1, c2)


def checkpoint(store: StorePaths, sha: str, sessions: list[str], config: Config = Config()) -> Path:
    return write_checkpoint(store, CheckpointRecord(sha=sha, author="t@example.com",
                                                    session_ids=sessions), config)


def run_json(world: World, capsys, *argv: str) -> dict:
    code, out, _ = world.ctx(capsys, *argv, "--json")
    assert code == 0, out
    return json.loads(out)

