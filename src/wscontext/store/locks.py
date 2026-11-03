"""Portable lock file: O_CREAT | O_EXCL, holding owner PID and timestamp."""

import os
import time
from pathlib import Path


class LockTimeout(Exception):
    pass


class FileLock:
    def __init__(self, path: Path, stale_after: float, timeout: float = 10.0,
                 poll: float = 0.02):
        self.path = Path(path)
        self.stale_after = stale_after
        self.timeout = timeout
        self.poll = poll

    def _stale(self) -> bool:
        try:
            return time.time() - self.path.stat().st_mtime > self.stale_after
        except FileNotFoundError:
            return False

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._stale():
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise LockTimeout(str(self.path)) from None
                time.sleep(self.poll)
                continue
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()} {time.time()}\n")
            return self

    def __exit__(self, *exc) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
