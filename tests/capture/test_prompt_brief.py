import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "index"))
from index_support import store  # noqa: F401  (fixture)
from support import hook, transcript  # noqa: F401  (fixtures)
from test_prompt_hits import PROMPT, build as build_index

from wscontext.capture import brief, dispatch, worker
from wscontext.capture.state import read_json, turn_state_path
from wscontext.cli.output import FOOTER, estimate_tokens
from wscontext.store.records import read_session

SECRET_WORDS = "zebrafish-quartz-9931"


def settings(store, text):
    (store.root / ".wscontext").mkdir(exist_ok=True)
    (store.root / ".wscontext" / "config.toml").write_text(text)


def prompt(store, text=PROMPT, sid="sess-new"):
    payload = {"session_id": sid, "cwd": str(store.root), "prompt": text,
               "hook_event_name": "UserPromptSubmit"}
    return dispatch.run("UserPromptSubmit", json.dumps(payload))


def test_first_prompt_gets_a_brief_inside_the_quoted_data_frame(store):
    build_index(store)
    entry = prompt(store)
    out = json.loads(entry["output"])["hookSpecificOutput"]
    assert out["hookEventName"] == "UserPromptSubmit"
    text = out["additionalContext"]
    assert text.startswith("RETRIEVED DATA from wscontext history") and text.endswith(FOOTER)
    assert "[session keys]" in text and "`ctx show keys`" in text
    assert estimate_tokens(text) <= 300
    state = read_json(turn_state_path(store, "sess-new"))
    assert state["prompt_brief_tokens"] == estimate_tokens(text)
    assert "keys" in state["prompt_brief_sessions"]
    assert len(state["turns"]) == 1


def test_wrapper_tags_are_not_searched_and_the_current_session_is_left_out(store):
    build_index(store)
    entry = prompt(store, "<ide_opened_file>invoice exporter</ide_opened_file> " + PROMPT,
                   sid="keys")
    text = json.loads(entry["output"])["hookSpecificOutput"]["additionalContext"]
    assert "[session keys]" not in text and "[session keys2]" in text


def test_second_prompt_gets_nothing(store):
    build_index(store)
    assert "output" in prompt(store)
    entry = prompt(store)
    assert entry["ok"] and "output" not in entry
    assert len(read_json(turn_state_path(store, "sess-new"))["turns"]) == 2


@pytest.mark.parametrize("text", ["[retrieval]\nprompt_brief = false\n",
                                  "[retrieval]\nenabled = false\n"])
def test_nothing_when_the_prompt_brief_or_retrieval_is_off(store, text):
    build_index(store)
    settings(store, text)
    entry = prompt(store)
    assert entry["ok"] and "output" not in entry
    assert "prompt_brief_tokens" not in read_json(turn_state_path(store, "sess-new"))


def test_brief_switch_does_not_turn_the_prompt_brief_off(store):
    build_index(store)
    settings(store, "[retrieval]\nbrief = false\n")
    assert "output" in prompt(store)


def test_missing_index_means_no_brief_and_no_error(store):
    entry = prompt(store)
    assert entry["ok"] and "output" not in entry
    log = store.logs / "hooks.jsonl"
    assert '"ok": false' not in log.read_text() and "error" not in log.read_text()


def test_unrelated_prompt_gets_nothing(store):
    build_index(store)
    entry = prompt(store, "Add a dark theme to the settings screen with a toggle")
    assert entry["ok"] and "output" not in entry


def test_dispatch_prints_the_output_json(store, monkeypatch, capsys):
    build_index(store)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "s1", "cwd": str(store.root), "prompt": PROMPT})))
    assert dispatch.main(["UserPromptSubmit"]) == 0
    printed = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert printed["hookEventName"] == "UserPromptSubmit"


def test_prompt_text_is_not_written_anywhere(store):
    build_index(store)
    prompt(store, PROMPT + " " + SECRET_WORDS)
    for path in store.root.rglob("*"):
        if path.is_file() and ".git" not in path.parts and "index" not in path.parts \
                and "raw" not in path.parts:
            assert SECRET_WORDS.encode() not in path.read_bytes(), path


def test_error_while_building_the_brief_still_records_the_turn(store, monkeypatch):
    build_index(store)
    monkeypatch.setattr(brief, "build_prompt", lambda *a: 1 / 0)
    entry = prompt(store)
    assert entry["ok"] and "output" not in entry
    assert len(read_json(turn_state_path(store, "sess-new"))["turns"]) == 1


def test_record_carries_the_prompt_brief_fields(hook, monkeypatch):
    store = hook.store
    hook("SessionStart", "session_start")
    monkeypatch.setattr(brief, "build_prompt", lambda *a: ("text", ["abc", "def"]))
    hook("UserPromptSubmit", "user_prompt_submit")
    hook("SessionEnd", "session_end")
    record = read_session(store, "sess-cap1")
    assert record.prompt_brief_tokens == estimate_tokens("text")
    assert record.prompt_brief_sessions == ["abc", "def"]
