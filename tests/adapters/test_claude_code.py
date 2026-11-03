import shutil
from pathlib import Path

from wscontext.adapters.base import TranscriptAdapter
from wscontext.adapters.claude_code import ClaudeCodeAdapter

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "transcripts"
adapter: TranscriptAdapter = ClaudeCodeAdapter()


def test_detect():
    assert adapter.detect((FIX / "known" / "sess1.jsonl").read_text().splitlines())
    assert not adapter.detect((FIX / "not_claude.jsonl").read_text().splitlines())
    assert not adapter.detect([])


def test_known_version_turns_and_tokens(tmp_path):
    shutil.copytree(FIX / "known", tmp_path / "k")
    p = adapter.parse(tmp_path / "k" / "sess1.jsonl")
    assert p.supported and p.format_version == "2.0.5"
    assert p.skipped_lines == 2
    assert [t.prompt for t in p.turns] == ["add a retry to the fetcher", "now add a test"]
    first, second = p.turns
    assert first.tool_calls == {"Read": 2, "Task": 1}
    # message m1 is split over two lines and counted once, with the later usage
    assert first.tokens == {"input": 30, "output": 12, "cache_read": 100}
    assert first.started_at == "2026-01-01T10:00:00Z"
    assert first.ended_at == "2026-01-01T10:00:09Z"
    assert second.tool_calls == {} and second.tokens is None
    assert second.ended_at == "2026-01-01T10:01:04Z"
    assert len(p.events) == 10


def test_subagents_located_relative_to_session(tmp_path):
    shutil.copytree(FIX / "known", tmp_path / "k")
    p = adapter.parse(tmp_path / "k" / "sess1.jsonl")
    assert [(r.agent_id, r.path) for r in p.subagents] == [
        ("abc123", tmp_path / "k" / "sess1" / "subagents" / "agent-abc123.jsonl")]


def test_missing_subagent_file_gives_no_path(tmp_path):
    shutil.copy(FIX / "known" / "sess1.jsonl", tmp_path / "sess1.jsonl")
    p = adapter.parse(tmp_path / "sess1.jsonl")
    assert [(r.agent_id, r.path) for r in p.subagents] == [("abc123", None)]


def test_unknown_version_degrades():
    p = adapter.parse(FIX / "unknown_version.jsonl")
    assert not p.supported and p.format_version == "9.1.0"
    assert p.turns == [] and p.events == [] and p.subagents == []


def test_missing_version_is_unsupported():
    p = adapter.parse(FIX / "no_version.jsonl")
    assert not p.supported and p.format_version is None


def test_empty_file(tmp_path):
    (tmp_path / "e.jsonl").write_bytes(b"")
    assert not adapter.parse(tmp_path / "e.jsonl").supported
