"""Hand-written Claude Code transcripts in the fixture projects folder, for `ctx import` tests."""

import json
import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from conftest import run_git

from wscontext.adapters import claude_home

DOCS = {"docs/adr.md": "# Decisions\n\nWe chose the quokka protocol.\n",
        "notes/Design-notes.MD": "# Notes\n\nplain text\n", "README.md": "# Readme\n",
        "CLAUDE.md": "rules\n"}


def commit_file(repo: Path, name: str, message: str) -> tuple[str, str]:
    """Commit a change to `name`; returns the new SHA and Git's own output."""
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text("x\n")
    run_git(repo, "add", name)
    out = subprocess.run(["git", "commit", "-m", message], cwd=repo, check=True,
                         capture_output=True, text=True).stdout
    return run_git(repo, "rev-parse", "HEAD").strip(), out


def add_docs(repo: Path) -> None:
    for path, text in DOCS.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-q", "-m", "docs")


def transcript(sid: str, cwd: Path, prompt: str, *, commit_output: str | None = None,
               start: str = "2026-01-01T10:00:00+00:00", version: str = "2.0.5",
               age_days: float = 3) -> Path:
    """One session: a prompt, a reply and optionally a `git commit` with Git's real output."""
    at = datetime.fromisoformat(start)
    lines: list[dict] = []

    def add(kind: str, message: dict) -> None:
        nonlocal at
        lines.append({"type": kind, "uuid": f"u{len(lines)}", "sessionId": sid, "cwd": str(cwd),
                      "gitBranch": "main", "version": version,
                      "timestamp": at.isoformat().replace("+00:00", "Z"), "message": message})
        at += timedelta(minutes=1)

    add("user", {"role": "user", "content": prompt})
    add("assistant", {"id": "m1", "role": "assistant", "content": [{"type": "text", "text": "ok"}]})
    if commit_output is not None:
        add("assistant", {"id": "m2", "role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": "git commit -m work"}}]})
        add("user", {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": commit_output}]})
    path = claude_home.projects_dir() / "-proj" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path
