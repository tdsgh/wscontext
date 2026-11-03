import json
import os

import pytest

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
    path = write_session(store, rec)
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
    path = write_checkpoint(store, rec)
    assert path == store.records / "checkpoints" / f"{'ab' * 20}.json"
    assert read_checkpoint(store, "ab" * 20) == rec


def test_turn_round_trip(tmp_path):
    rec = TurnRecord(session_id="s-1", author="x", index=3, started_at="t")
    assert read_record(write_record(tmp_path / "t.json", rec), TurnRecord) == rec


def test_output_is_deterministic_and_leaves_no_temp_files(store):
    a = write_session(store, _session()).read_bytes()
    b = write_session(store, _session()).read_bytes()
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
        write_session(store, SessionRecord(session_id=bad, author="x"))
    assert not store.records.exists()


def test_failed_write_keeps_previous_file(store, monkeypatch):
    path = write_session(store, _session())
    before = path.read_bytes()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(records.os, "replace", boom)
    changed = _session()
    changed.branch = "other"
    with pytest.raises(OSError):
        write_session(store, changed)
    assert path.read_bytes() == before
    assert os.listdir(path.parent) == ["s-1.json"]
