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


def test_turn_items_in_order():
    p = adapter.parse(FIX / "full.jsonl")
    assert p.supported and len(p.turns) == 1
    items = p.turns[0].items
    assert [i.kind for i in items] == [
        "prompt", "text", "tool_call", "tool_result", "tool_call", "tool_call",
        "tool_result", "tool_result", "subagent", "text"]
    assert items[0].text == "first line of a long prompt\nsecond line"
    assert p.turns[0].prompt == items[0].text
    assert items[1].text == "On it."
    assert (items[2].name, items[2].tool_id, items[2].text) == (
        "Bash", "t1", "command=ls -la, description=list")
    assert (items[3].tool_id, items[3].text, items[3].truncated) == ("t1", "a.py\nb.py", False)
    assert items[5].name == "Task"
    assert items[8].agent_id == "sub1"
    assert items[9].text == "Done."
    assert "sidechain words" not in [i.text for i in items]


def test_long_prompt_item_is_full_while_preview_is_short(tmp_path):
    long = "x" * 500
    line = ('{"type":"user","version":"2.1.314","message":{"role":"user","content":"%s"}}' % long)
    (tmp_path / "l.jsonl").write_text(line + "\n")
    t = adapter.parse(tmp_path / "l.jsonl").turns[0]
    assert len(t.prompt) == 200 and t.items[0].text == long


def test_huge_tool_result_is_truncated_with_original_size():
    items = adapter.parse(FIX / "full.jsonl").turns[0].items
    big = items[6]
    assert big.truncated and big.size == 3000 and len(big.text) == 2000


def test_tool_input_value_is_shortened():
    items = adapter.parse(FIX / "full.jsonl").turns[0].items
    assert items[5].text == "description=explore, prompt=look around"


def test_unsupported_version_has_no_items():
    p = adapter.parse(FIX / "unknown_version.jsonl")
    assert p.turns == []


def test_main_parse_still_skips_sidechain_lines():
    p = adapter.parse(FIX / "subagent.jsonl")
    assert p.supported and p.turns == []


def test_subagent_file_parses_into_turn_and_items():
    p = adapter.parse(FIX / "subagent.jsonl", sidechain=True)
    assert p.supported and len(p.turns) == 1
    items = p.turns[0].items
    assert [i.kind for i in items] == [
        "prompt", "text", "tool_call", "tool_result", "tool_call", "tool_result",
        "subagent", "text"]
    assert items[0].text == "explore the repo and report"
    assert items[2].name == "Grep" and items[2].text == "pattern=retry"
    assert items[6].agent_id == "nest1"
    assert items[7].text == "Report: retry is in a.py."
    assert p.turns[0].tool_calls == {"Grep": 1, "Task": 1}


def test_subagent_parse_lists_only_nested_agents():
    p = adapter.parse(FIX / "subagent.jsonl", sidechain=True)
    assert [r.agent_id for r in p.subagents] == ["nest1"]


def test_subagent_parse_of_unknown_version_is_unsupported():
    p = adapter.parse(FIX / "unknown_version.jsonl", sidechain=True)
    assert not p.supported and p.turns == []
