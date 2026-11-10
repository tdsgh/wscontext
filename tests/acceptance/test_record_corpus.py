"""The corpus recorder's keep-going, resume and corpus filtering, with claude and ctx faked."""

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from conftest import put_store_file, run_git

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "acceptance"))
import record_corpus as rc  # noqa: E402
from stage1 import Acc, Claude, Fail  # noqa: E402

BANNED = {"marker": "NEVER-IN-THE-STORE"}


@dataclass
class FakeAcc(Acc):
    """claude writes a file per task and, as told by `behaviour[message]`, commits it, makes no
    commit, commits under another message, or times out; it plants the store files hooks write."""
    behaviour: dict[str, str] = field(default_factory=dict)
    calls: list = field(default_factory=list)

    def ctx(self, repo, *args, code=0):
        self.calls.append(("ctx", *args))
        return subprocess.CompletedProcess(args, 0, "", "")

    def hook(self, repo, event, sid):
        self.calls.append(("hook", event, sid))
        path = self.store(repo) / "state" / "active.json"
        path.write_text(json.dumps({k: {s: t for s, t in v.items() if s != sid}
                                    for k, v in json.loads(path.read_text()).items()}))

    def wait_ended(self, repo, sid):
        return {"id": sid}

    def claude(self, repo, prompt, session=None, resume=False, tools=""):
        message = re.search(r'acc-commit "([^"]*)"', prompt)[1]
        local = self.store(repo)
        put_store_file(local, f"records/sessions/{session}.json", json.dumps({"session_id": session}).encode())
        put_store_file(local, f"raw/sessions/2026-11-06-{session}.jsonl", b"{}\n")
        put_store_file(local, f"raw/subagents/{session}/agent-1.jsonl", b"{}\n")
        (local / "logs").mkdir(exist_ok=True)
        with (local / "logs" / "hooks.jsonl").open("a") as log:
            log.write(json.dumps({"session_id": session, "event": "Stop"}) + "\n")
        (repo / f"{message.replace(' ', '-').lower()}.txt").write_text(message + "\n")
        behaviour = self.behaviour[message]
        if behaviour == "timeout":
            put_store_file(local, "state/active.json", json.dumps({"wt": {session: "2026-11-06T00:00:00+00:00"}}).encode())
            put_store_file(local, f"state/turns/{session}.json", b"{}")
            raise Fail("claude -p ran longer than 300s")
        if behaviour in ("ok", "other"):
            run_git(repo, "add", "-A")
            run_git(repo, "commit", "-q", "-m", message if behaviour == "ok" else "Something else")
            sha = run_git(repo, "rev-parse", "HEAD").strip()
            put_store_file(local, f"records/checkpoints/{sha}.json", json.dumps({"session_ids": [session]}).encode())
        return Claude(session, False, "Done.", ["Write", "Bash"])


def task(n: int, branch: str = "main", merge: bool = False) -> rc.Task:
    return rc.Task(id=f"t{n}", kind="feature", prompt=f"Do step {n}.", message=f"Step {n} done",
                   branch=branch, merge=merge)


STORY = [task(1), task(2), task(3, "feat"), task(4, "feat", merge=True), task(5)]


def new_acc(tmp_path: Path, behaviour: dict[str, str]) -> FakeAcc:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_EDITOR": "true"}
    return FakeAcc(tmp_path, None, None, 1.0, env, behaviour=behaviour)


def subjects(repo: Path, rev: str) -> list[str]:
    return run_git(repo, "log", "--first-parent", "--format=%s", rev).splitlines()


def played(tmp_path: Path) -> tuple[FakeAcc, Path, dict]:
    """t1 ok, t2 no commit, t3 timed out on feat, t4 committed under another message and merged, t5 ok."""
    acc = new_acc(tmp_path, {"Step 1 done": "ok", "Step 2 done": "nocommit", "Step 3 done": "timeout",
                             "Step 4 done": "other", "Step 5 done": "ok"})
    record = {"version": 1, "tasks_file": "tasks.toml", "plugin_data": "", "tasks": []}
    repo = rc.setup_repo(acc)
    rc.play(acc, repo, STORY, BANNED, record)
    return acc, repo, record


def test_a_failed_task_is_recorded_and_its_work_committed(tmp_path):
    acc, repo, record = played(tmp_path)
    outcomes = [(e["id"], e["outcome"]) for e in record["tasks"]]
    assert outcomes == [("t1", "ok"), ("t2", "failed"), ("t3", "failed"), ("t4", "failed"), ("t5", "ok")]
    reasons = [e["reason"] for e in record["tasks"]]
    assert reasons[1] == "no commit 'Step 2 done'"
    assert reasons[2].startswith("claude: claude -p ran longer than") and "no commit 'Step 3 done'" in reasons[2]
    assert reasons[3] == "committed as 'Something else', not 'Step 4 done'"
    assert record["tasks"][1]["reply"] == "Done." and record["tasks"][1]["tools"] == ["Write", "Bash"]
    # the recorder committed t2 and t3; t4's own commit is not committed again
    assert subjects(repo, "main") == ["Step 5 done", "Merge feat", "Step 2 done", "Step 1 done",
                                      "Start the tally project"]
    assert subjects(repo, "feat")[:3] == ["Something else", "Step 3 done", "Step 2 done"]
    assert "step-3-done.txt" in run_git(repo, "show", "--name-only", "--format=", ":/Step 3 done")
    # only the killed session needed its SessionEnd run for it
    assert [c for c in acc.calls if c[0] == "hook"] == [("hook", "SessionEnd", record["tasks"][2]["session"])]
    assert json.loads((tmp_path / rc.STATE).read_text()) == record
    assert record["tasks"][2]["heads"] == {"main": run_git(repo, "rev-parse", ":/Step 2 done").strip()}
    assert set(record["tasks"][3]["heads"]) == {"main", "feat"}


def test_a_leak_in_the_store_stops_the_run_and_records_why(tmp_path):
    acc = new_acc(tmp_path, {"Step 1 done": "ok", "Step 2 done": "ok"})
    record = {"version": 1, "tasks_file": "tasks.toml", "plugin_data": "", "tasks": []}
    repo = rc.setup_repo(acc)
    with pytest.raises(Fail, match="private data"):
        rc.play(acc, repo, STORY[:2], {"marker": "session_id"}, record)
    saved = json.loads((tmp_path / rc.STATE).read_text())
    assert [(e["id"], e["outcome"]) for e in saved["tasks"]] == [("t1", "failed")]
    assert "private data" in saved["tasks"][0]["reason"]


def test_resume_point_defaults_to_the_first_failure_and_checks_the_story(tmp_path):
    acc, repo, record = played(tmp_path)
    assert rc.resume_point(record, STORY, None) == 1
    # t3 created feat: rewinding to it deletes the branch; rewinding to t4 resets it
    assert rc.plan_rewind(acc, repo, record, 2).deleted == ["feat"]
    assert rc.plan_rewind(acc, repo, record, 3).deleted == []
    assert rc.resume_point(record, STORY, "t4") == 3
    assert rc.resume_point(record, STORY + [task(6)], "t6") == 5
    with pytest.raises(Fail, match="not a recorded task"):
        rc.resume_point(record, STORY, "t9")
    with pytest.raises(Fail, match="no longer starts with"):
        rc.resume_point(record, [task(1), task(7), *STORY[2:]], "t4")
    edited = [STORY[0], rc.Task(id="t2", kind="bugfix", prompt="Fixed story.", message="Step 2 done")] + STORY[2:]
    assert rc.resume_point(record, edited, None) == 1


def test_resume_rewinds_branches_and_store_then_only_accepted_sessions_reach_the_corpus(tmp_path):
    acc, repo, record = played(tmp_path)
    store = acc.store(repo)
    put_store_file(store, "records/sessions/stray.json", b"{}")
    put_store_file(store, "records/.leftover.json.123.tmp", b"")
    put_store_file(store, "state/rewrites/1-2.txt", b"")
    put_store_file(store, f"state/locks/worker-{record['tasks'][3]['session']}.lock", b"")
    rc.rebuild_history(acc, repo, store)  # what the workers committed to the history ref
    kept = record["tasks"][0]["session"]

    k = rc.resume_point(record, STORY, None)
    plan = rc.plan_rewind(acc, repo, record, k)
    assert plan.keep == {kept} and plan.deleted == ["feat"]
    assert set(plan.sessions) == {e["session"] for e in record["tasks"][1:]} | {"stray"}
    rc.apply_rewind(acc, repo, plan)

    assert rc.heads(acc, repo) == record["tasks"][1]["heads"]
    assert run_git(repo, "status", "--porcelain") == ""
    t1_commit = record["tasks"][0]["commit"]
    left = sorted(p.relative_to(store).as_posix() for p in rc.store_files(store))
    assert left == [f"raw/sessions/2026-11-06-{kept}.jsonl", f"raw/subagents/{kept}/agent-1.jsonl",
                    f"records/checkpoints/{t1_commit}.json", f"records/sessions/{kept}.json"]
    on_ref = run_git(repo, "ls-tree", "-r", "--name-only", rc.HISTORY_REF).split()
    assert sorted(on_ref) == left
    assert sorted(p.name for p in (store / "state").rglob("*") if p.is_file()) == []
    logged = [json.loads(line)["session_id"] for line in (store / "logs" / "hooks.jsonl").read_text().splitlines()]
    assert logged == [kept]

    # the story was fixed; every task now commits
    del record["tasks"][k:]
    acc.behaviour = dict.fromkeys(acc.behaviour, "ok")
    rc.play(acc, repo, STORY, BANNED, record)
    assert not rc.failures(record)
    assert subjects(repo, "main")[:2] == ["Step 5 done", "Merge feat"]
    assert subjects(repo, "feat")[:2] == ["Step 4 done", "Step 3 done"]
    put_store_file(store, "records/sessions/late-stray.json", b"{}")
    put_store_file(store, "raw/sessions/2026-11-06-late-stray.jsonl", b"{}\n")

    left_out = rc.stage_corpus(acc, repo, record, tmp_path / "staged")
    accepted = sorted(e["session"] for e in record["tasks"])
    corpus = tmp_path / "staged" / "corpus"
    assert sorted(p.stem for p in (corpus / "records" / "sessions").glob("*.json")) == accepted
    assert sorted(p.name for p in (corpus / "raw" / "subagents").iterdir()) == accepted
    assert sorted(p.stem for p in (corpus / "records" / "checkpoints").glob("*.json")) == sorted(
        e["commit"] for e in record["tasks"])
    assert left_out == ["raw/sessions/2026-11-06-late-stray.jsonl", "records/sessions/late-stray.json"]
    listed = run_git(repo, "bundle", "list-heads", str(tmp_path / "staged" / "repo.bundle"))
    assert sorted(line.split()[1] for line in listed.splitlines()) == ["HEAD", "refs/heads/feat", "refs/heads/main"]


def test_a_story_commit_linked_to_a_rejected_session_fails_the_corpus(tmp_path):
    acc = new_acc(tmp_path, {"Step 1 done": "ok"})
    record = {"version": 1, "tasks_file": "tasks.toml", "plugin_data": "", "tasks": []}
    repo = rc.setup_repo(acc)
    rc.play(acc, repo, STORY[:1], BANNED, record)
    sha = record["tasks"][0]["commit"]
    put_store_file(acc.store(repo), f"records/checkpoints/{sha}.json",
                   json.dumps({"session_ids": [record["tasks"][0]["session"], "rewound"]}).encode())
    with pytest.raises(Fail, match="no accepted task ran"):
        rc.stage_corpus(acc, repo, record, tmp_path / "staged")


# --- re-trimming the recorded corpus ---

from wscontext.adapters.claude_code import reduce  # noqa: E402
from wscontext.capture.worker import copy_reduced, stored_sizes  # noqa: E402
from wscontext.config import Config, RedactionConfig  # noqa: E402
from wscontext.redact.redactor import redact_bytes  # noqa: E402
from wscontext.store.records import SessionRecord, read_record, write_record  # noqa: E402

KNOWN = Path(__file__).resolve().parents[1] / "fixtures" / "transcripts" / "known"
SID = "5e55a0a0-0000-4000-8000-000000000001"
OWNER = json.dumps({"type": "session-owner", "sessionId": SID, "accountUuid": "acc-0001",
                    "organizationUuid": "org-0001"}) + "\n"
CONFIG = Config(redaction=RedactionConfig(extra_patterns=(r"STORY-SECRET-\d+",)))


def recorded(tmp_path: Path) -> tuple[Path, Path]:
    """A recorded corpus of one session with a subagent, stored before session-owner lines were
    bookkeeping, its source transcripts, and a bundle whose committed config adds a redaction rule."""
    repo = tmp_path / "story"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    (repo / ".wscontext").mkdir()
    (repo / ".wscontext" / "config.toml").write_text('[redaction]\nextra_patterns = ["STORY-SECRET-\\\\d+"]\n')
    run_git(repo, "add", "-A")
    run_git(repo, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "story")
    out = tmp_path / "out"
    (out / "corpus").mkdir(parents=True)
    run_git(repo, "bundle", "create", "-q", str(out / "repo.bundle"), "HEAD", "--branches")
    src = tmp_path / "source"
    (src / SID / "subagents").mkdir(parents=True)
    prompt = json.dumps({"type": "user", "version": "2.0.5", "timestamp": "2026-01-01T10:02:00Z",
                         "message": {"role": "user", "content": "use STORY-SECRET-42"}}) + "\n"
    (src / f"{SID}.jsonl").write_text((KNOWN / "sess1.jsonl").read_text() + OWNER + prompt)
    agent = src / SID / "subagents" / "agent-abc123.jsonl"
    agent.write_text((KNOWN / "sess1" / "subagents" / "agent-abc123.jsonl").read_text() + OWNER)
    corpus = out / "corpus"
    raw = corpus / "raw" / "sessions" / f"2026-11-06-{SID}.jsonl"
    copy_reduced(src / f"{SID}.jsonl", raw, CONFIG)
    raw.write_bytes(raw.read_bytes() + OWNER.encode())  # what the old list kept
    (corpus / "raw" / "subagents" / SID).mkdir(parents=True)
    (corpus / "raw" / "subagents" / SID / "abc123.jsonl").write_bytes(agent.read_bytes())
    source_bytes = (src / f"{SID}.jsonl").stat().st_size + agent.stat().st_size
    rec = SessionRecord(session_id=SID, author="corpus@example.invalid", worktree="/tmp/tally",
                        started_at="2026-11-06T10:00:00+00:00", head_end="abc",
                        transcript_bytes={"prompt": 1, "unknown": len(OWNER), "stored": 9, "source": source_bytes})
    write_record(corpus / "records" / "sessions" / f"{SID}.json", rec, Config())
    (corpus / "records" / "checkpoints").mkdir()
    (corpus / "records" / "checkpoints" / "abc.json").write_text('{"session_ids": ["%s"]}\n' % SID)
    return out, src


def files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_retrim_stores_the_copies_as_a_fresh_capture_would(tmp_path, capsys):
    out, src = recorded(tmp_path)
    before = files(out)
    assert rc.main(["--retrim", str(src), "--out", str(out)]) == 0
    after = files(out)
    assert sorted(after) == sorted(before)
    raw, agent = f"corpus/raw/sessions/2026-11-06-{SID}.jsonl", f"corpus/raw/subagents/{SID}/abc123.jsonl"
    # the bundle's committed redaction rule applies, and nothing else differs from a capture
    assert after[raw] == redact_bytes(reduce((src / f"{SID}.jsonl").read_bytes()), CONFIG)[0]
    assert b"STORY-SECRET-42" not in after[raw]
    assert after[agent] == reduce((src / SID / "subagents" / "agent-abc123.jsonl").read_bytes())
    for name in (raw, agent):
        assert b"accountUuid" not in after[name] and b"organizationUuid" not in after[name]
    for name in ("repo.bundle", "corpus/records/checkpoints/abc.json"):
        assert after[name] == before[name]
    sizes = read_record(out / "corpus" / "records" / "sessions" / f"{SID}.json", SessionRecord).transcript_bytes
    assert sizes == {**stored_sizes(out / raw, out / "corpus" / "raw" / "subagents" / SID),
                     "source": sizes["source"]}
    assert sizes["stored"] == len(after[raw]) + len(after[agent])
    assert sizes["source"] == (src / f"{SID}.jsonl").stat().st_size + (src / SID / "subagents" / "agent-abc123.jsonl").stat().st_size
    assert sum(v for k, v in sizes.items() if k not in ("stored", "source")) == sizes["stored"]
    first = json.loads(before[f"corpus/records/sessions/{SID}.json"])
    assert {k: v for k, v in json.loads(after[f"corpus/records/sessions/{SID}.json"]).items()
            if k != "transcript_bytes"} == {k: v for k, v in first.items() if k != "transcript_bytes"}
    assert "re-trimmed 1 sessions and 1 subagent copies" in capsys.readouterr().out


def test_retrim_writes_nothing_when_a_source_changed_after_the_capture(tmp_path, capsys):
    out, src = recorded(tmp_path)
    with (src / f"{SID}.jsonl").open("a") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "resumed"}}) + "\n")
    before = files(out)
    assert rc.main(["--retrim", str(src), "--out", str(out)]) == 1
    assert "changed after the capture" in capsys.readouterr().err
    assert files(out) == before


def test_the_privacy_scan_rejects_account_identifier_keys_whatever_their_value(tmp_path):
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "a.jsonl").write_text(json.dumps({"type": "x", "organizationUuid": "any"}) + "\n")
    (tmp_path / "raw" / "b.jsonl").write_text(json.dumps({"type": "x", "content": "no keys here"}) + "\n")
    assert rc.scan_files(tmp_path, rc.forbidden()) == ["account identifier (organizationUuid) in raw/a.jsonl"]
