"""Build small stores for the index tests: records, raw copies and real commits."""

import json
from pathlib import Path

import pytest

from conftest import run_git
from wscontext.config import Config
from wscontext.store.paths import StorePaths
from wscontext.store.records import (CheckpointRecord, SessionRecord, write_checkpoint,
                                     write_session)


def transcript(*turns: tuple[str, str, str], sidechain_id: str | None = None) -> bytes:
    """Turns of (prompt, reply, tool command) as a Claude Code transcript."""
    lines = []
    for n, (prompt, reply, command) in enumerate(turns):
        ts = f"2026-02-01T09:{n:02d}:00Z"
        lines.append({"type": "user", "version": "2.1.314", "timestamp": ts,
                      "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}})
        blocks = [{"type": "text", "text": reply}]
        if command:
            blocks.append({"type": "tool_use", "id": f"t{n}", "name": "Bash",
                           "input": {"command": command}})
        lines.append({"type": "assistant", "version": "2.1.314", "timestamp": ts,
                      "message": {"id": f"m{n}", "role": "assistant", "content": blocks}})
    return ("\n".join(json.dumps(x) for x in lines) + "\n").encode()


@pytest.fixture
def store(git_repo: Path) -> StorePaths:
    return StorePaths.resolve(git_repo)


def add_session(store: StorePaths, sid: str, turns, *, branch="main", author="ann",
                files=(), date="2026-02-01", ended=True, raw=True, subagents=None) -> Path:
    rec = SessionRecord(session_id=sid, author=author, branch=branch,
                        started_at=f"{date}T09:00:00Z",
                        ended_at=f"{date}T10:00:00Z" if ended else None,
                        files_touched=list(files))
    write_session(store, rec, Config())
    path = store.raw / "sessions" / f"{date}-{sid}.jsonl"
    if raw:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(transcript(*turns))
    for agent, agent_turns in (subagents or {}).items():
        sub = store.raw / "subagents" / sid / f"{agent}.jsonl"
        sub.parent.mkdir(parents=True, exist_ok=True)
        sub.write_bytes(transcript(*agent_turns))
    return path


def add_commit(store: StorePaths, message: str, sessions: list[str], author="ann") -> str:
    repo = store.root
    (repo / f"f{len(message)}{abs(hash(message)) % 10**6}.txt").write_text(message)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", message)
    sha = run_git(repo, "rev-parse", "HEAD").strip()
    write_checkpoint(store, CheckpointRecord(sha=sha, author=author, session_ids=sessions,
                                             created_at="2026-02-01T09:30:00Z"), Config())
    return sha
