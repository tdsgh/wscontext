import json
from pathlib import Path

from wscontext.adapters.claude_code import ClaudeCodeAdapter

adapter = ClaudeCodeAdapter()


def _write(path: Path, lines: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def _call(tid, name, inp, ts, side=False):
    return {"type": "assistant", "version": "2.0.5", "timestamp": ts, "isSidechain": side,
            "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def _result(tid, text, ts, side=False):
    return {"type": "user", "timestamp": ts, "isSidechain": side,
            "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]}}


def test_whole_input_and_result_and_missing_result(tmp_path):
    command = "git commit -m " + "x" * 200
    path = _write(tmp_path / "s.jsonl", [
        _call("t1", "Bash", {"command": command}, "T1"),
        _result("t1", "y" * 5000, "T2"),
        _call("t2", "Read", {"file_path": "a"}, "T3"),
    ])
    first, second = adapter.tool_uses(path)
    assert first.input["command"] == command
    assert first.result == "y" * 5000 and first.timestamp == "T2"
    assert second.result is None and second.timestamp == "T3"


def test_result_given_as_text_blocks(tmp_path):
    path = _write(tmp_path / "s.jsonl", [
        _call("t1", "Bash", {}, "T1"),
        _result("t1", [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], "T2"),
    ])
    assert adapter.tool_uses(path)[0].result == "a\nb"


def test_sidechain_lines(tmp_path):
    path = _write(tmp_path / "s.jsonl", [
        _call("t1", "Bash", {}, "T1"),
        _call("t2", "Grep", {}, "T2", side=True),
        _result("t2", "r", "T3", side=True),
    ])
    assert [u.name for u in adapter.tool_uses(path)] == ["Bash"]
    both = adapter.tool_uses(path, sidechain=True)
    assert [(u.name, u.result) for u in both] == [("Bash", None), ("Grep", "r")]


def test_unsupported_version_and_garbage(tmp_path):
    bad = _call("t1", "Bash", {}, "T1")
    bad["version"] = "9.0.0"
    assert adapter.tool_uses(_write(tmp_path / "a.jsonl", [bad])) == []
    mixed = tmp_path / "b.jsonl"
    mixed.write_text("not json\n" + json.dumps(_call("t1", "Bash", {}, "T1")) + "\n")
    assert len(adapter.tool_uses(mixed)) == 1
