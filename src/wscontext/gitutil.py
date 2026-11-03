"""The single subprocess wrapper for Git."""

import subprocess
from pathlib import Path


class GitError(Exception):
    def __init__(self, args: tuple[str, ...], returncode: int, stderr: str):
        super().__init__(f"git {' '.join(args)} failed ({returncode}): {stderr.strip()}")
        self.args_run = args
        self.returncode = returncode
        self.stderr = stderr


def git_bytes(*args: str, cwd: Path, input: bytes | None = None) -> bytes:
    """Run Git on raw bytes: no newline translation in either direction."""
    # Without input, stdin is closed so Git never reads a hook's own stdin.
    stdin = {"stdin": subprocess.DEVNULL} if input is None else {"input": input}
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, **stdin)
    if proc.returncode != 0:
        raise GitError(args, proc.returncode, proc.stderr.decode("utf-8", "replace"))
    return proc.stdout


def git(*args: str, cwd: Path, input: str | None = None) -> str:
    data = None if input is None else input.encode("utf-8")
    return git_bytes(*args, cwd=cwd, input=data).decode("utf-8")
