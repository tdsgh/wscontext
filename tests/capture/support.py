import json
import shutil
from pathlib import Path

import pytest

from wscontext.capture import dispatch, worker
from wscontext.store.paths import StorePaths

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    """A hand-written transcript with a fake secret in it, copied outside the repo."""
    dest = tmp_path / "claude" / "sess-cap1.jsonl"
    dest.parent.mkdir()
    src = FIXTURES / "transcripts" / "known"
    shutil.copy(src / "sess1.jsonl", dest)
    with dest.open("a") as f:
        f.write(json.dumps({"type": "user", "version": "2.0.5", "timestamp": "2026-01-01T10:02:00Z",
                            "message": {"role": "user", "content":
                                        "token ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"}})
                + "\n")
    shutil.copytree(src / "sess1", dest.parent / "sess-cap1")
    return dest


@pytest.fixture
def hook(git_repo: Path, transcript: Path, monkeypatch):
    """Fire a recorded hook input through the dispatcher; workers run inline."""
    monkeypatch.setattr(worker, "spawn_worker", lambda args, cwd: worker.run(*args))
    store = StorePaths.resolve(git_repo)

    def fire(event: str, fixture: str) -> dict:
        text = (FIXTURES / "hooks" / f"{fixture}.json").read_text()
        text = text.replace("{cwd}", json.dumps(str(git_repo))[1:-1]) \
                   .replace("{transcript}", json.dumps(str(transcript))[1:-1])
        return dispatch.run(event, text)

    fire.store = store
    return fire
