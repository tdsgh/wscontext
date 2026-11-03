import json
import os

import pytest

from wscontext.config import Config
from wscontext.store import records
from wscontext.store.paths import StorePaths
from wscontext.store.records import (CheckpointRecord, RecordError, SessionRecord, TurnRecord,
                                     read_checkpoint, read_record, read_session,
                                     write_checkpoint, write_record, write_session)


@pytest.fixture
def store(git_repo):
    return StorePaths.resolve(git_repo)


def _session() -> SessionRecord:
    turn = TurnRecord(session_id="s-1", author="Test <t@example.com>", index=0,
                      started_at="2026-11-03T10:00:00Z", ended_at="2026-11-03T10:01:00Z",
                      tool_calls={"Read": 2}, tokens={"input_tokens": 10})
    return SessionRecord(session_id="s-1", author="Test <t@example.com>", branch="main",
                         started_at="2026-11-03T10:00:00Z", files_touched=["a.txt"],
                         subagents=["ag-1"], turns=[turn], tool_calls={"Read": 2},
                         ctx_calls={"why": 1})


def test_every_record_carries_version_and_author():
    for cls in (SessionRecord, TurnRecord, CheckpointRecord):
        names = {f for f in cls.__dataclass_fields__}
        assert {"schema_version", "author"} <= names


def test_session_round_trip(store):
    rec = _session()
    path = write_session(store, rec, Config())
    assert path == store.records / "sessions" / "s-1.json"
    back = read_session(store, "s-1")
    assert back == rec
    assert isinstance(back.turns[0], TurnRecord)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1 and data["author"] == "Test <t@example.com>"
    assert data["turns"][0]["schema_version"] == 1


def test_checkpoint_round_trip(store):
    rec = CheckpointRecord(sha="ab" * 20, author="Test <t@example.com>", session_ids=["s-1"],
                           files_changed=1, insertions=3, rewritten_from="cd" * 20)
    path = write_checkpoint(store, rec, Config())
    assert path == store.records / "checkpoints" / f"{'ab' * 20}.json"
    assert read_checkpoint(store, "ab" * 20) == rec


def test_turn_round_trip(tmp_path):
    rec = TurnRecord(session_id="s-1", author="x", index=3, started_at="t")
    assert read_record(write_record(tmp_path / "t.json", rec, Config()), TurnRecord) == rec


def test_output_is_deterministic_and_leaves_no_temp_files(store):
    a = write_session(store, _session(), Config()).read_bytes()
    b = write_session(store, _session(), Config()).read_bytes()
    assert a == b and a.endswith(b"\n") and b"\r\n" not in a
    assert os.listdir(store.records / "sessions") == ["s-1.json"]


def test_minimal_earlier_record_reads_with_defaults(tmp_path):
    p = tmp_path / "s.json"
    p.write_text('{"schema_version": 1, "session_id": "s-9", "author": "x", "later": 1}')
    rec = read_record(p, SessionRecord)
    assert rec.session_id == "s-9" and rec.turns == [] and rec.tokens is None


@pytest.mark.parametrize("version", [2, 0, None, "1"])
def test_unsupported_schema_version_is_rejected(tmp_path, version):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"schema_version": version, "session_id": "s", "author": "x"}))
    with pytest.raises(RecordError):
        read_record(p, SessionRecord)


def test_missing_required_field_is_rejected(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"schema_version": 1, "sha": "ab", "author": "x"}')
    with pytest.raises(RecordError):
        read_record(p, CheckpointRecord)


@pytest.mark.parametrize("bad", ["../x", "a/b", "", ".hidden", "a\\b"])
def test_unsafe_names_are_rejected(store, bad):
    with pytest.raises(RecordError):
        write_session(store, SessionRecord(session_id=bad, author="x"), Config())
    assert not store.records.exists()


def test_failed_write_keeps_previous_file(store, monkeypatch):
    path = write_session(store, _session(), Config())
    before = path.read_bytes()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(records.os, "replace", boom)
    changed = _session()
    changed.branch = "other"
    with pytest.raises(OSError):
        write_session(store, changed, Config())
    assert path.read_bytes() == before
    assert os.listdir(path.parent) == ["s-1.json"]


SECRET = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def test_session_record_never_stores_a_secret(store):
    rec = SessionRecord(session_id="s-1", author=f"me {SECRET}", branch=f"feat/{SECRET}",
                        worktree=f"/work/{SECRET}", files_touched=[f"src/{SECRET}.py"],
                        subagents=["ag-1"], tool_calls={"Read": 2})
    path = write_session(store, rec, Config())
    raw = path.read_bytes()
    assert SECRET.encode() not in raw
    assert b"[REDACTED:github-token]" in raw
    back = read_session(store, "s-1")
    assert back.branch == "feat/[REDACTED:github-token]"
    assert back.files_touched == ["src/[REDACTED:github-token].py"]
    assert back.tool_calls == {"Read": 2} and back.session_id == "s-1"


def test_checkpoint_never_stores_a_secret(store):
    rec = CheckpointRecord(sha="ab" * 20, author=f"x {SECRET}", session_ids=["s-1"])
    raw = write_checkpoint(store, rec, Config()).read_bytes()
    assert SECRET.encode() not in raw
    assert read_checkpoint(store, "ab" * 20).sha == "ab" * 20


def test_multiline_secret_is_redacted_before_json_escaping(store):
    key = "-----BEGIN RSA PRIVATE KEY-----\nMIIEabc123\n-----END RSA PRIVATE KEY-----"
    path = write_session(store, SessionRecord(session_id="s-1", author="x", branch=key), Config())
    assert b"MIIEabc123" not in path.read_bytes()
    assert read_session(store, "s-1").branch == "[REDACTED:private-key]"


def test_custom_pattern_applies_to_records(store):
    from wscontext.config import RedactionConfig
    config = Config(redaction=RedactionConfig(extra_patterns=("corp-[0-9]{6}",)))
    path = write_session(store, SessionRecord(session_id="s-1", author="x", branch="corp-123456"),
                         config)
    assert b"corp-123456" not in path.read_bytes()


def test_state_file_never_stores_a_secret(store):
    from wscontext.capture.state import read_json, update_json
    path = store.state / "turns" / "s-1.json"
    update_json(path, lambda d: d.update(files=[f"src/{SECRET}.py"], note=SECRET,
                                         nested={SECRET: [SECRET]}, count=3), Config())
    assert SECRET.encode() not in path.read_bytes()
    state = read_json(path)
    assert state["files"] == ["src/[REDACTED:github-token].py"] and state["count"] == 3
    assert not list(path.parent.glob("*.tmp"))
