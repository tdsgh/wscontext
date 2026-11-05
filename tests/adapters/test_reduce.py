import json
import shutil
from dataclasses import asdict
from pathlib import Path

import pytest

from wscontext.adapters.claude_code import ClaudeCodeAdapter, entry_kind, measure, reduce

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "transcripts"
SHAPED = FIX / "stored_shape.jsonl"  # shaped like a real session: bookkeeping, duplicates
FIXTURES = sorted(p.relative_to(FIX) for p in FIX.rglob("*.jsonl"))


def view(path: Path, root: Path, sidechain: bool) -> tuple:
    p = ClaudeCodeAdapter().parse(path, sidechain=sidechain)
    return (p.supported, p.format_version, [asdict(t) for t in p.turns],
            [(r.agent_id, r.path and r.path.relative_to(root)) for r in p.subagents])


def line(**obj) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode() + b"\n"


def objects(data: bytes) -> list[dict]:
    out = []
    for raw in data.splitlines():
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


@pytest.mark.parametrize("rel", FIXTURES, ids=str)
@pytest.mark.parametrize("sidechain", [False, True])
def test_parse_of_reduced_copy_equals_parse_of_original(rel, sidechain, tmp_path):
    shutil.copytree(FIX, tmp_path / "orig")
    shutil.copytree(FIX, tmp_path / "reduced")
    target = tmp_path / "reduced" / rel
    target.write_bytes(reduce(target.read_bytes()))
    assert view(target, tmp_path / "reduced", sidechain) == \
        view(tmp_path / "orig" / rel, tmp_path / "orig", sidechain)


def test_reduced_copy_is_below_the_gate_share():
    assert len(reduce(SHAPED.read_bytes())) < 0.30 * SHAPED.stat().st_size


def test_bookkeeping_goes_and_every_other_kind_stays():
    data = (FIX / "kinds.jsonl").read_bytes()
    before = [entry_kind(o, sidechain=True) for o in objects(data)]
    after = [entry_kind(o, sidechain=True) for o in objects(reduce(data))]
    assert "bookkeeping" not in after
    assert after == [k for k in before if k != "bookkeeping"]


def test_unknown_and_unparseable_lines_survive_byte_for_byte():
    data = (FIX / "kinds.jsonl").read_bytes()
    kept = reduce(data).splitlines(keepends=True)
    for raw in data.splitlines(keepends=True):
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = None
        if obj is None or entry_kind(obj) == "unknown":
            assert raw in kept, raw


def test_unknown_field_is_kept_and_named_fields_are_dropped():
    data = line(type="assistant", version="2.1.0", sessionId="s", uuid="r1",
                someNewField={"x": 1}, wireToolInputs={"t1": {}}, requestId="req",
                message={"id": "m1", "role": "assistant", "stop_reason": "end_turn",
                         "content": [{"type": "thinking", "thinking": "hm", "signature": "SIG"},
                                     {"type": "text", "text": "hi"}],
                         "usage": {"input_tokens": 1, "output_tokens": 2, "iterations": [],
                                   "cache_creation": {}}})
    [obj] = objects(reduce(data))
    assert obj["someNewField"] == {"x": 1}
    assert "wireToolInputs" not in obj and "requestId" not in obj
    assert obj["message"]["content"][0] == {"type": "thinking", "thinking": "hm"}
    assert obj["message"]["usage"] == {"input_tokens": 1, "output_tokens": 2}
    assert obj["message"]["stop_reason"] == "end_turn"


def test_tool_use_result_keeps_only_the_agent_id():
    content = [{"type": "tool_result", "tool_use_id": "t1", "content": "done"}]
    data = line(type="user", uuid="a", message={"role": "user", "content": content},
                toolUseResult={"agentId": "ag1", "content": "done", "totalTokens": 9},
                sourceToolAssistantUUID="r1", permissionDecision={"behavior": "allow"}) + \
        line(type="user", uuid="b", message={"role": "user", "content": content},
             toolUseResult={"stdout": "done", "stderr": ""}) + \
        line(type="user", uuid="c", message={"role": "user", "content": content},
             toolUseResult="Error: denied")
    a, b, c = objects(reduce(data))
    assert a["toolUseResult"] == {"agentId": "ag1"}
    assert "sourceToolAssistantUUID" not in a and "permissionDecision" not in a
    assert "toolUseResult" not in b and "toolUseResult" not in c


def test_meta_origin_loses_its_copies_of_the_message():
    data = line(type="user", isMeta=True, uuid="m", message={"role": "user", "content": "report"},
                origin={"kind": "task-notification", "body": "report",
                        "handbackReportText": "report", "name": "agent"}) + \
        line(type="attachment", uuid="f", attachment={"type": "file", "filename": "a.py"},
             rendered="1\tx", renderedRole="user", renderedInHumanTurn=True)
    meta, attachment = objects(reduce(data))
    assert meta["origin"] == {"kind": "task-notification", "name": "agent"}
    assert attachment == {"type": "attachment", "uuid": "f",
                          "attachment": {"type": "file", "filename": "a.py"}}


def test_keep_once_fields_reappear_when_their_value_changes():
    data = b"".join(
        line(type="user", sessionId="s", cwd=cwd, version="2.1.0", uuid=str(i),
             message={"role": "user", "content": f"prompt {i}"})
        for i, cwd in enumerate(["/a", "/a", "/a/sub", "/a/sub", "/a"]))
    rows = objects(reduce(data))
    assert [r.get("cwd") for r in rows] == ["/a", None, "/a/sub", None, "/a"]
    assert [("sessionId" in r, "version" in r) for r in rows] == [(True, True)] + [(False, False)] * 4
    assert [r["uuid"] for r in rows] == ["0", "1", "2", "3", "4"]


def test_a_keep_once_field_on_an_unknown_line_stays_and_counts_as_stored():
    data = line(type="brand-new-entry", sessionId="s", cwd="/a") + \
        line(type="user", sessionId="s", cwd="/a", message={"role": "user", "content": "hi"})
    unknown, prompt = objects(reduce(data))
    assert unknown == {"type": "brand-new-entry", "sessionId": "s", "cwd": "/a"}
    assert "sessionId" not in prompt and "cwd" not in prompt


def test_a_title_is_kept_only_when_it_changes():
    data = b"".join(line(type="ai-title", sessionId="s", aiTitle=t) for t in "AABBA")
    assert [o["aiTitle"] for o in objects(reduce(data))] == ["A", "B", "A"]


def test_unchanged_lines_keep_their_bytes_and_line_endings():
    data = b'{"type": "user", "message": {"content": "spaced json"}}\r\n'
    assert reduce(data) == data


def test_a_prefix_reduces_to_a_prefix():
    data = SHAPED.read_bytes()
    full = reduce(data)
    lines = data.splitlines(keepends=True)
    for n in (1, 5, len(lines) // 2, len(lines) - 1):
        assert full.startswith(reduce(b"".join(lines[:n])))


def test_reducing_twice_changes_nothing():
    once = reduce(SHAPED.read_bytes(), 50)
    assert reduce(once, 50) == once


def result_line(content) -> bytes:
    return line(type="assistant", version="2.1.0", message={
        "id": "m1", "role": "assistant",
        "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]}) + \
        line(type="user", version="2.1.0", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": content}]})


def test_cut_tool_result_is_marked_and_still_parses(tmp_path):
    prompt = line(type="user", version="2.1.0", message={"role": "user", "content": "go"})
    path = tmp_path / "cut.jsonl"
    path.write_bytes(reduce(prompt + result_line("x" * 500), 100))
    [item] = [i for i in ClaudeCodeAdapter().parse(path).turns[0].items if i.kind == "tool_result"]
    assert item.text == "x" * 100 + "\n[wscontext: tool result cut at 100 of 500 characters]"
    assert measure(path)["tool_result lines"] == 1 and "unknown" not in measure(path)


def test_cut_joins_text_blocks_and_keeps_other_blocks(tmp_path):
    image = {"type": "image", "source": {"type": "base64", "data": "AAAA"}}
    content = [{"type": "text", "text": "a" * 60}, image, {"type": "text", "text": "b" * 60}]
    [_, result] = objects(reduce(result_line(content), 100))
    cut = result["message"]["content"][0]["content"]
    assert cut == [{"type": "text", "text": "a" * 60 + "\n" + "b" * 39
                    + "\n[wscontext: tool result cut at 100 of 121 characters]"}, image]


def test_short_results_and_zero_are_left_whole():
    data = result_line("x" * 500)
    assert reduce(data, 500) == reduce(data, 0)
    assert objects(reduce(data, 0))[1]["message"]["content"][0]["content"] == "x" * 500
