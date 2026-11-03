import os
import time

import pytest

from wscontext.store.locks import FileLock, LockTimeout


def test_contention(tmp_path):
    p = tmp_path / "x.lock"
    with FileLock(p, stale_after=60):
        assert p.exists()
        with pytest.raises(LockTimeout):
            with FileLock(p, stale_after=60, timeout=0.1):
                pass
    assert not p.exists()
    with FileLock(p, stale_after=60):
        pass


def test_stale_lock_is_broken(tmp_path):
    p = tmp_path / "x.lock"
    p.write_text("1 0\n")
    old = time.time() - 1000
    os.utime(p, (old, old))
    with FileLock(p, stale_after=10, timeout=0.5):
        assert str(os.getpid()) in p.read_text()
