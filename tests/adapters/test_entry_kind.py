import json
from pathlib import Path

from wscontext.adapters.claude_code import entry_kind, measure

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "transcripts" / "kinds.jsonl"

# one per fixture line that parses as a JSON object, in file order
EXPECTED = [
    "prompt", "reply", "tool_result", "meta", "meta",
    "meta", "meta", "meta", "meta",
    "compaction", "compaction", "title", "title", "subagent",
    *["bookkeeping"] * 11,
    *["bookkeeping"] * 21,
    "unknown", "unknown", "unknown",
]


def objects() -> list[dict]:
    out = []
    for line in FIXTURE.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def test_every_fixture_line_classified_as_listed():
    assert [entry_kind(o) for o in objects()] == EXPECTED


def test_sidechain_line_is_classified_by_shape_inside_a_subagent_file():
    line = next(o for o in objects() if o.get("isSidechain"))
    assert entry_kind(line) == "subagent"
    assert entry_kind(line, sidechain=True) == "reply"


def test_bookkeeping_stays_bookkeeping_on_a_sidechain_line():
    assert entry_kind({"type": "api-request", "isSidechain": True}) == "bookkeeping"


def test_measure_totals_match_the_file_and_hold_no_values():
    sizes = measure(FIXTURE)
    kinds = {k for k in sizes if " " not in k and "." not in k}
    assert sum(sizes[k] for k in kinds) == FIXTURE.stat().st_size
    assert sum(sizes[f"{k} lines"] for k in kinds) == len(FIXTURE.read_bytes().splitlines())
    assert sizes["bookkeeping lines"] == 32 and sizes["unknown lines"] == 4
    assert not any(k.startswith("bookkeeping.") for k in sizes)
    assert sizes["reply.wireToolInputs"] == len('"wireToolInputs":{"t1":{"file_path":"fetch.py"}}')
    # keys are kind and field names only; no value from the transcript leaks into them
    assert not any(word in key for key in sizes
                   for word in ("retry", "fetch", "k1", "summary of", "Read"))
