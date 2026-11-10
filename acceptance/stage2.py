"""Stage-2 acceptance run: the capabilities of docs/hist/wscontext-phase-2.md §4 to §11 against the installed plugin.

    .venv/bin/python acceptance/stage2.py [--only NAME ...] [--no-claude] [--list] [--keep]

Not part of the offline suite (pytest never collects `acceptance/`). Scenarios that need a real
session drive headless Claude Code (`claude -p`) and spend API usage; `--no-claude` runs only the
ones that do not. The others drive the real hook entrypoint with hand-written hook inputs, as
stage 1 does. Every repository, bare remote and clone lives under one temporary directory; `ctx
init`, `ctx sync` and `git push` only ever target paths inside it.

It reuses the plugin lookup, the headless-session driver and the throwaway-repository helpers of
`stage1.py`, which sits beside it, and runs `ctx` through the installed plugin's own launcher.
"""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stage1 import (COMMIT_SCRIPT, Acc, Fail, Manual, check, init_repo, locate_plugin,
                    wait_for)

HISTORY_REF = "refs/wscontext/history"
LEGACY_REF = "refs/heads/wscontext/history"
EMAIL = "acceptance@example.invalid"  # the identity Acc.repo configures
IDLE_WINDOW_MINUTES = 1
BRIEF_BUDGET_TOKENS = 500
SHARE_GATE = 0.30  # stored-to-source share the stage gate allows (WP1 gate outcome)


# --- helpers -----------------------------------------------------------------------------------

def unique_word() -> str:
    return "zq" + uuid.uuid4().hex[:8]


def hook(acc: Acc, repo: Path, event: str, sid: str, **extra) -> str:
    """Run the real hook entrypoint with a hand-written input; returns what it printed."""
    payload = {"session_id": sid, "cwd": str(repo), "transcript_path": "", "hook_event_name": event, **extra}
    return acc.run([*acc.plugin.ctx, "hook", event], repo, stdin=json.dumps(payload), timeout=60).stdout


def brief_given(out: str) -> str | None:
    """The brief a SessionStart hook run handed to the agent, or None when it printed nothing."""
    if not out.strip():
        return None
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def ctx_tool(acc: Acc, repo: Path, sid: str, command: str) -> None:
    hook(acc, repo, "PostToolUse", sid, tool_name="Bash", tool_input={"command": command},
         tool_response={"stdout": "ok"})


def synthetic_session(acc: Acc, repo: Path, sid: str, source: str = "startup") -> None:
    """A whole session from hook inputs alone: start, one prompt, stop, end."""
    hook(acc, repo, "SessionStart", sid, source=source)
    hook(acc, repo, "UserPromptSubmit", sid, prompt="hello")
    hook(acc, repo, "Stop", sid)
    hook(acc, repo, "SessionEnd", sid)
    acc.wait_ended(repo, sid)


def set_config(repo: Path, section: str, key: str, value: str) -> None:
    """Set `key = value` (a TOML literal) under `[section]` of the repository's config."""
    path = repo / ".wscontext" / "config.toml"
    out, current = [], None
    for line in path.read_text().splitlines():
        head = re.fullmatch(r"\[(\w+)\]\s*", line)
        if head:
            current = head.group(1)
        if current == section and re.match(rf"{key}\s*=", line):
            line = f"{key} = {value}"
        out.append(line)
    text = "\n".join(out) + "\n"
    check(f"{key} = {value}" in text, f"[{section}] {key} is not in {path}")
    path.write_text(text)


def hits(acc: Acc, repo: Path, *args: str) -> list[dict]:
    return acc.ctx_json(repo, "find", *args)["results"]


def stats_data(acc: Acc, repo: Path) -> dict:
    return acc.ctx_json(repo, "stats", "data")["results"][0]


def stats_session(acc: Acc, repo: Path, sid: str) -> dict:
    return acc.ctx_json(repo, "stats", "session", sid)["results"][0]


def doctor_item(acc: Acc, repo: Path, name: str) -> dict:
    checks = acc.ctx_json(repo, "doctor", code=None)["checks"]
    return next(c for c in checks if c["check"] == name)


def sweep_now(acc: Acc, repo: Path) -> None:
    """The sweep worker, run to completion in the foreground instead of detached."""
    acc.ctx(repo, "worker", "sweep", "", "", str(repo))


def raw_copies(acc: Acc, repo: Path, sid: str) -> list[Path]:
    return list((acc.store(repo) / "raw" / "sessions").glob(f"*-{sid}.jsonl"))


def remote_refs(acc: Acc, repo: Path) -> str:
    return acc.git(repo, "ls-remote", "origin")


def shared_repo(acc: Acc, name: str) -> tuple[Path, Path]:
    """A repository with sharing on and a bare remote that already holds its main branch."""
    bare = acc.tmp / f"{name}-remote.git"
    bare.mkdir()
    acc.git(bare, "init", "-q", "--bare", "-b", "main")
    repo = acc.repo(name)
    init_repo(acc, repo)
    acc.git(repo, "remote", "add", "origin", str(bare))
    acc.git(repo, "add", "-A")
    acc.git(repo, "commit", "-q", "-m", "wscontext config")
    acc.git(repo, "push", "-q", "origin", "main")
    acc.ctx(repo, "sync", "--setup", "--remote", "origin")
    acc.git(repo, "add", ".wscontext/config.toml")
    acc.git(repo, "commit", "-q", "-m", "share history")
    return bare, repo


def push_session(acc: Acc, repo: Path, sid: str) -> None:
    """Push main so the pre-push hook sends the history, and wait until the remote has it."""
    acc.wait_ended(repo, sid)
    acc.checkpoint(repo, acc.git(repo, "rev-parse", "HEAD"))
    acc.git(repo, "push", "-q", "origin", "main")
    wait_for(lambda: HISTORY_REF in remote_refs(acc, repo), f"the pre-push hook to push {HISTORY_REF}")


# --- scenarios ---------------------------------------------------------------------------------

def find_own(acc: Acc) -> str:
    repo = acc.repo("find")
    init_repo(acc, repo)
    word = unique_word()
    sid = acc.commit_session(repo, "ledger.py", "def total(): return 0", f"add {word} ledger",
                             marker=f"Project note {word}.").session_id
    acc.wait_ended(repo, sid)
    sha = acc.git(repo, "rev-parse", "HEAD")
    acc.checkpoint(repo, sha)
    # no rebuild and no wait: a search takes in what was captured before it ran
    found = hits(acc, repo, word)
    check(any(h["type"] == "session" and h["id"] == sid for h in found), f"find misses session {sid}: {found}")
    check(any(h["type"] == "commit" and h["sha"] == sha for h in found), f"find misses commit {sha[:12]}")
    shown = acc.ctx(repo, "find", word).stdout
    check("RETRIEVED DATA" in shown and f"ctx show {sid}" in shown and f"git show {sha[:12]}" in shown,
          f"find output lacks the frame or the links: {shown[:400]}")
    check(all(h["type"] == "commit" for h in hits(acc, repo, word, "--kind", "commit")), "--kind commit let a session in")
    only = hits(acc, repo, word, "--kind", "session")
    check(only and all(h["type"] == "session" for h in only), f"--kind session: {only}")
    check(hits(acc, repo, word, "--branch", "main"), "--branch main lost the hits")
    check(not hits(acc, repo, word, "--branch", "no-such-branch"), "--branch no-such-branch still found something")
    check(hits(acc, repo, word, "--author", EMAIL), f"--author {EMAIL} found nothing")
    check(not hits(acc, repo, word, "--author", "nobody@example.invalid"), "--author nobody still found something")
    check(hits(acc, repo, word, "--since", "1d"), "--since 1d found nothing")
    check(not hits(acc, repo, word, "--since", "2999-01-01"), "--since in the future still found something")
    check(any(h["id"] == sid for h in hits(acc, repo, word, "--file", "ledger.py")), "--file ledger.py misses the session")
    check(not hits(acc, repo, word, "--file", "other.py"), "--file other.py still found something")
    limited = acc.ctx_json(repo, "--limit", "1", "find", word)
    check(len(limited["results"]) == 1 and limited["omitted"] >= 1, "--limit 1 did not cut the answer")
    acc.ctx(repo, "forget", sid)
    check(not any(h["type"] == "session" and h["id"] == sid for h in hits(acc, repo, word)),
          "a forgotten session is still found")
    return f"session and commit found at once; filters narrow; --limit cuts; forgotten session gone from search ({sid[:8]})"


def find_teammate(acc: Acc) -> str:
    _, repo = shared_repo(acc, "mate1")
    word = unique_word()
    sid = acc.commit_session(repo, "shared.py", "print(1)", f"add {word} shared",
                             marker=f"Shared note {word}.").session_id
    push_session(acc, repo, sid)
    second = acc.tmp / "mate2"
    acc.git(acc.tmp, "clone", "-q", str(acc.tmp / "mate1-remote.git"), str(second))
    acc.identify(second)
    acc.ctx(second, "init")
    found = wait_for(lambda: [h for h in hits(acc, second, word) if h["type"] == "session" and h["id"] == sid],
                     "the second clone to find the first one's session", 60)
    check(found[0]["branch"] == "main" and found[0]["author"] == EMAIL, f"hit lacks branch or author: {found[0]}")
    check(word in acc.ctx(second, "show", sid).stdout, "ctx show in the second clone lacks the prompt")
    return f"clone 2 found session {sid[:8]} synced from clone 1, with branch and author"


def brief(acc: Acc) -> str:
    empty = acc.repo("brief-empty")
    init_repo(acc, empty)
    check(brief_given(hook(acc, empty, "SessionStart", "empty-" + uuid.uuid4().hex[:8], source="startup")) is None,
          "a repository with no history got a brief")
    repo = acc.repo("brief")
    init_repo(acc, repo)
    word = unique_word()
    first = acc.commit_session(repo, "widget.py", "def widget(): return 1", f"add {word} widget",
                               marker=f"Brief note {word}.").session_id
    acc.wait_ended(repo, first)
    acc.checkpoint(repo, acc.git(repo, "rev-parse", "HEAD"))
    shown = wait_for(lambda: (t := acc.ctx(repo, "brief").stdout) and "RETRIEVED DATA" in t and t,
                     "ctx brief to show the first session")
    check(first[:8] in shown and "Recent sessions on branch main" in shown, f"ctx brief lacks the session: {shown[:400]}")
    sid = "brief-" + uuid.uuid4().hex[:8]
    for source in ("startup", "clear", "compact"):
        text = brief_given(hook(acc, repo, "SessionStart", sid, source=source))
        check(text and first[:8] in text, f"no brief at source {source}: {text!r}")
        check(-(-len(text) // 4) <= BRIEF_BUDGET_TOKENS, f"the brief at {source} is over {BRIEF_BUDGET_TOKENS} tokens")
    for source in ("resume", "fork"):
        check(brief_given(hook(acc, repo, "SessionStart", sid, source=source)) is None, f"a brief was given at {source}")
    set_config(repo, "retrieval", "brief", "false")
    check(brief_given(hook(acc, repo, "SessionStart", sid, source="startup")) is None, "a brief was given with the brief off")
    check("switched off" in acc.ctx(repo, "brief").stdout, "ctx brief does not say the brief is off")
    check(hits(acc, repo, word), "ctx find stopped working when only the brief was switched off")
    set_config(repo, "retrieval", "brief", "true")
    hook(acc, repo, "SessionEnd", sid)
    done = wait_for(lambda: (d := stats_session(acc, repo, sid)) and d["ended_by"] and d, f"session {sid} to end")
    check(done["retrieval"] is True and done["brief_tokens"] > 0,
          f"session record: retrieval {done['retrieval']}, brief_tokens {done['brief_tokens']}")
    return f"brief at startup, clear and compact within budget; none at resume, fork, empty repo or brief off ({done['brief_tokens']} tokens recorded)"


def triggers(acc: Acc) -> str:
    repo = acc.repo("triggers")
    old = ("<!-- wscontext:begin -->\n## wscontext\n\nPast agent sessions in this repository are captured and "
           "linked to commits.\nBefore changing code you did not write, run `ctx why <file>:<line>` to see the\n"
           "session that produced it.\n<!-- wscontext:end -->\n")
    (repo / "CLAUDE.md").write_text(f"# mine\n\n{old}\ntrailing text\n")
    acc.ctx(repo, "init")
    text = (repo / "CLAUDE.md").read_text()
    check("ctx find" in text and "ctx why" in text, "the re-initialized block lacks a trigger")
    check(text.count("<!-- wscontext:begin -->") == 1, "re-init duplicated the block")
    check(text.startswith("# mine") and text.rstrip().endswith("trailing text"), f"own text around the block lost: {text!r}")
    check(len(text.split("<!-- wscontext:begin -->")[1].split("<!-- wscontext:end -->")[0]) < 600, "the block grew into a manual")
    item = doctor_item(acc, repo, "claude.md block")
    check(item["status"] == "ok", f"doctor on the upgraded block: {item}")
    return "re-init replaced a stage-1 block in place with the search trigger, kept the text around it, doctor ok"


def idle(acc: Acc) -> str:
    repo = acc.repo("idle")
    init_repo(acc, repo)
    set_config(repo, "capture", "stale_session_minutes", str(IDLE_WINDOW_MINUTES))
    sid = "idle-" + uuid.uuid4().hex[:8]
    # hook inputs of a conversation that is never ended: no SessionEnd
    hook(acc, repo, "SessionStart", sid, source="startup")
    hook(acc, repo, "UserPromptSubmit", sid, prompt="work")
    hook(acc, repo, "Stop", sid)
    last = time.monotonic()

    def status() -> dict:
        return next(s for s in acc.ctx_json(repo, "sessions", "--full")["results"] if s["id"] == sid)

    check(status()["status"] == "running", "a fresh session is not listed as running")
    time.sleep(3)
    check(status()["status"] == "running", "a session was finalized inside the window")
    time.sleep(max(0, IDLE_WINDOW_MINUTES * 60 + 5 - (time.monotonic() - last)))
    acc.ctx(repo, "sessions")  # the next wscontext run after the window finalizes it
    done = wait_for(lambda: (s := status())["status"] == "finalized-idle" and s, "the idle session to be finalized", 60)
    check(done["turns"] == 1, f"idle-finalized session has {done['turns']} turns, wanted 1")
    ended = datetime.fromisoformat(done["ended_at"])
    ended = ended if ended.tzinfo else ended.replace(tzinfo=timezone.utc)
    check(ended < datetime.now(timezone.utc) - timedelta(seconds=IDLE_WINDOW_MINUTES * 60 - 10),
          f"ended_at {done['ended_at']} is not the last activity")
    check(stats_session(acc, repo, sid)["ended_by"] == "idle", "record does not say it ended by going idle")
    # resumed: active again, linked to a new commit, earlier counts kept
    hook(acc, repo, "UserPromptSubmit", sid, prompt="more work")
    hook(acc, repo, "Stop", sid)
    acc.git(repo, "commit", "--allow-empty", "-q", "-m", "after resume")
    check(acc.trailers(repo) == [sid], f"commit after the resume has trailers {acc.trailers(repo)}")
    hook(acc, repo, "SessionEnd", sid)
    final = wait_for(lambda: (d := stats_session(acc, repo, sid)) and d["ended_by"] == "session-end" and d,
                     "the resumed session to end")
    check(status()["status"] == "finished", "the resumed session is not listed as finished")
    turns = len(acc.ctx_json(repo, "stats", "session", sid)["results"][1]["turns"])
    check(turns == 2, f"resumed session holds {turns} turns, wanted the first one kept plus the new one")
    check(final["retrieval"] is True, "retrieval switch not recorded")
    return "idle past the window, finalized by the next ctx call with ended_by idle; resumed, linked to a commit, 2 turns"


def volume(acc: Acc) -> str:
    repo = acc.repo("volume")
    init_repo(acc, repo)
    word = unique_word()
    sid = acc.commit_session(repo, "volume.py", "print('volume')", f"add {word} volume",
                             marker=f"Volume note {word}.").session_id
    acc.wait_ended(repo, sid)
    data = stats_data(acc, repo)
    t = data["transcript"]
    check(t["stored"] > 0 and t["source"], f"no stored or source bytes recorded: {t}")
    share = t["stored_of_known_source"] / t["source"]
    check(share <= SHARE_GATE, f"stored {share:.1%} of the source transcript, over the {SHARE_GATE:.0%} gate")
    check(t["kinds"], "no bytes by kind")
    volume_of = stats_session(acc, repo, sid)["volume"]
    check(volume_of["stored"] == t["stored"] and volume_of["kinds"], f"session volume {volume_of['stored']} vs data {t['stored']}")
    check(data["warnings"] == [] and doctor_item(acc, repo, "size limits")["status"] == "ok", "a warning with default limits")
    set_config(repo, "store", "session_warn_bytes", "100")
    warned = stats_data(acc, repo)["warnings"]
    check(any(w["scope"] == "session" and w["id"] == sid and w["largest"] for w in warned),
          f"no session warning naming what takes the space: {warned}")
    check(doctor_item(acc, repo, "size limits")["status"] == "warn", "doctor shows no size warning")
    set_config(repo, "store", "session_warn_bytes", "10485760")
    check(not stats_data(acc, repo)["warnings"], "the warning stayed after the limit was restored")
    # sync off: the raw copy is pruned once its blob is on the local history branch
    check(raw_copies(acc, repo, sid), "no raw copy to prune")
    set_config(repo, "retention", "raw_days", "0")
    sweep_now(acc, repo)
    check(not raw_copies(acc, repo, sid), "the raw copy was not pruned with sync off")
    check(stats_data(acc, repo)["pruned_paths"] >= 1, "stats does not count the pruned file")
    check(word in acc.ctx(repo, "show", sid).stdout, "ctx show lost the session after pruning")
    check(any(h["id"] == sid for h in hits(acc, repo, word)), "find lost the session after pruning")
    check(acc.ctx_json(repo, "sessions")["results"], "sessions lists nothing after pruning")
    return f"stored {share:.1%} of source; by-kind figures agree; warning names the session and clears; pruned raw stays readable and searchable"


def prune_shared(acc: Acc) -> str:
    _, repo = shared_repo(acc, "prune")
    word = unique_word()
    sid = acc.commit_session(repo, "p.py", "print(2)", f"add {word} p", marker=f"Prune note {word}.").session_id
    acc.wait_ended(repo, sid)
    set_config(repo, "retention", "raw_days", "0")
    sweep_now(acc, repo)
    check(raw_copies(acc, repo, sid), "the raw copy was pruned before it was shared")
    push_session(acc, repo, sid)
    acc.ctx(repo, "sync")
    sweep_now(acc, repo)
    check(not raw_copies(acc, repo, sid), "the raw copy was not pruned once shared")
    check(word in acc.ctx(repo, "show", sid).stdout, "ctx show lost the shared session after pruning")
    return "with sync on, the raw copy stayed until the history reached the remote, then was pruned and stayed readable"


def ref_move(acc: Acc) -> str:
    bare = acc.tmp / "move-remote.git"
    bare.mkdir()
    acc.git(bare, "init", "-q", "--bare", "-b", "main")
    old = acc.repo("move-old")
    init_repo(acc, old)
    acc.git(old, "remote", "add", "origin", str(bare))
    acc.git(old, "push", "-q", "origin", "main")
    sid = "move-" + uuid.uuid4().hex[:8]
    synthetic_session(acc, old, sid)
    tip = wait_for(lambda: acc.git(old, "rev-parse", "--verify", "-q", HISTORY_REF, ok=False),
                   f"the session's history commit on {HISTORY_REF}")
    # the stage-1 layout: history on a branch, here and on the remote
    acc.git(old, "update-ref", LEGACY_REF, tip)
    acc.git(old, "update-ref", "-d", HISTORY_REF)
    acc.git(old, "push", "-q", "origin", f"{LEGACY_REF}:{LEGACY_REF}")
    check(LEGACY_REF in remote_refs(acc, old), "could not build a stage-1-layout remote")
    check(sid in [s["id"] for s in acc.ctx_json(old, "sessions")["results"]], "sessions does not see history left on the stage-1 branch")
    check(acc.git(old, "rev-parse", "--verify", "-q", HISTORY_REF, ok=False) and
          not acc.git(old, "rev-parse", "--verify", "-q", LEGACY_REF, ok=False),
          "the local stage-1 branch did not move into the new ref")
    # a teammate on the new version: fetch reads the old branch, the push moves it
    mate = acc.tmp / "move-new"
    acc.git(acc.tmp, "clone", "-q", str(bare), str(mate))
    acc.identify(mate)
    acc.ctx(mate, "init")
    acc.ctx(mate, "sync", "--setup", "--remote", "origin")
    check(sid in [s["id"] for s in acc.ctx_json(mate, "sessions")["results"]], "the migrated clone does not see the stage-1 history")
    layout = doctor_item(acc, mate, "history ref")
    check(layout["status"] == "warn" and "stage-1" in layout["detail"], f"doctor does not flag the old branch: {layout}")
    out = acc.ctx(mate, "sync").stdout
    check("was removed from origin" in out, f"ctx sync did not say the old branch moved: {out}")
    refs = remote_refs(acc, mate)
    check(HISTORY_REF in refs and LEGACY_REF not in refs, f"remote refs after the move: {refs}")
    check(doctor_item(acc, mate, "history ref")["status"] == "ok", "doctor still flags the history ref after the move")
    acc.git(mate, "fetch", "-q", "--prune", "origin")
    check(acc.git(mate, "rev-parse", "--verify", "-q", "refs/wscontext/remotes/origin/history"),
          "a plain `git fetch --prune` deleted the tracking ref")
    plain = acc.tmp / "move-plain"
    acc.git(acc.tmp, "clone", "-q", str(bare), str(plain))
    check(not acc.git(plain, "for-each-ref", "refs/wscontext"), "a plain clone downloaded the history")
    check("wscontext" not in acc.git(plain, "branch", "-a"), "the history shows among a plain clone's branches")
    return "stage-1 branch moved locally; a stage-1 remote migrated on the next sync and says so; a plain clone and a pruning fetch see no history"


def switch_off(acc: Acc) -> str:
    repo = acc.repo("switch")
    init_repo(acc, repo)
    first = "switch-" + uuid.uuid4().hex[:8]
    synthetic_session(acc, repo, first)
    acc.ctx(repo, "find", "hello")  # builds the index while retrieval is on
    set_config(repo, "retrieval", "enabled", "false")
    out = acc.ctx(repo, "find", "hello", code=0).stdout
    check("switched off" in out, f"find with retrieval off: {out[:200]}")
    check(acc.ctx_json(repo, "find", "hello")["enabled"] is False, "find --json does not report the switch")
    check("switched off" in acc.ctx(repo, "brief").stdout, "ctx brief does not say retrieval is off")
    sid = "switch-" + uuid.uuid4().hex[:8]
    check(brief_given(hook(acc, repo, "SessionStart", sid, source="startup")) is None, "a brief was given with retrieval off")
    hook(acc, repo, "UserPromptSubmit", sid, prompt="work")
    hook(acc, repo, "Stop", sid)
    hook(acc, repo, "SessionEnd", sid)
    acc.wait_ended(repo, sid)
    done = wait_for(lambda: (d := stats_session(acc, repo, sid)) and d["ended_by"] and d, "the session record")
    check(done["retrieval"] is False and not done["brief_tokens"], f"record: retrieval {done['retrieval']}, brief {done['brief_tokens']}")
    data = stats_data(acc, repo)
    check(data["sessions"] == 2 and data["retrieval"]["enabled"] is False, f"stats data: {data['retrieval']}")
    check(len(acc.ctx_json(repo, "sessions")["results"]) == 2 and acc.ctx(repo, "why", "README.md:1").returncode == 0,
          "capture, sessions or why stopped with retrieval off")
    stale = doctor_item(acc, repo, "claude.md block")
    check(stale["status"] == "warn", f"doctor does not flag a block that does not match the switch: {stale}")
    acc.ctx(repo, "init")
    check("ctx find" not in (repo / "CLAUDE.md").read_text(), "init kept the search trigger with retrieval off")
    check(doctor_item(acc, repo, "claude.md block")["status"] == "ok", "doctor flags the block after re-init")
    set_config(repo, "retrieval", "enabled", "true")
    acc.ctx(repo, "init")
    check("ctx find" in (repo / "CLAUDE.md").read_text(), "init did not restore the search trigger")
    return "find and brief off, record says retrieval off, capture and stats unchanged, block follows the switch both ways"


def stats(acc: Acc) -> str:
    repo = acc.repo("stats2")
    init_repo(acc, repo)
    caller, quiet = "stats-" + uuid.uuid4().hex[:8], "stats-" + uuid.uuid4().hex[:8]
    hook(acc, repo, "SessionStart", caller, source="startup")
    hook(acc, repo, "UserPromptSubmit", caller, prompt="search")
    for command in ("ctx find alpha", "ctx find beta", "ctx why README.md:1"):
        ctx_tool(acc, repo, caller, command)
    hook(acc, repo, "Stop", caller)
    hook(acc, repo, "SessionEnd", caller)
    acc.wait_ended(repo, caller)
    wait_for(lambda: stats_data(acc, repo)["index"]["sessions"] >= 1, "the worker to index the first session")
    # the second session starts with history behind it: it gets a brief, the first did not
    text = brief_given(hook(acc, repo, "SessionStart", quiet, source="startup"))
    check(text and caller[:8] in text, f"no brief naming the first session: {text!r}")
    hook(acc, repo, "UserPromptSubmit", quiet, prompt="plain")
    hook(acc, repo, "Stop", quiet)
    hook(acc, repo, "SessionEnd", quiet)
    acc.wait_ended(repo, quiet)
    wait_for(lambda: stats_session(acc, repo, quiet)["ended_by"], "the second record")
    first = stats_session(acc, repo, caller)
    check(first["ctx_calls"] == {"find": 2, "why": 1} and first["tool_calls"] == {"ctx": 3},
          f"session ctx calls {first['ctx_calls']}, tool calls {first['tool_calls']}")
    check(not first["brief_tokens"], f"the first session records a brief of {first['brief_tokens']} tokens")
    check(stats_session(acc, repo, quiet)["brief_tokens"] == -(-len(text) // 4), "recorded brief size differs from the brief given")
    adoption = acc.ctx_json(repo, "stats", "queries")["results"][0]["adoption"]
    check((adoption["sessions"], adoption["with_ctx"], adoption["with_find"]) == (2, 1, 1), f"adoption: {adoption}")
    acc.ctx(repo, "find", "hello")
    data = stats_data(acc, repo)
    index, retrieval = data["index"], data["retrieval"]
    check(index["present"] and index["lag"] == 0 and index["sessions"] == data["sessions"] == 2, f"index figures: {index}")
    check(retrieval["sessions_with_brief"] == 1 and retrieval["sessions_with_switch"] == 2
          and retrieval["sessions_retrieval_on"] == 2, f"brief figures: {retrieval}")
    return "ctx calls, adoption, brief share and index size/lag agree with the two sessions that ran"


@dataclass
class Scenario:
    name: str
    run: Callable[[Acc], str]
    claude: bool
    what: str


SCENARIOS = [
    Scenario("find-own", find_own, True, "find over the session just captured and its commit; filters; limit; forget (§4)"),
    Scenario("find-teammate", find_teammate, True, "find over a session synced from another clone (§4)"),
    Scenario("brief", brief, True, "brief at startup, clear, compact; none at resume or off; budget (§5)"),
    Scenario("triggers", triggers, False, "re-init upgrades the CLAUDE.md block, keeps the user's text (§6)"),
    Scenario("idle", idle, False, "a session with no SessionEnd is finalized after the window, then resumed (§7)"),
    Scenario("volume", volume, True, "stored share, by-kind figures, size warning, pruning with sync off (§8)"),
    Scenario("prune-shared", prune_shared, True, "pruning with sync on waits for the remote (§8)"),
    Scenario("ref-move", ref_move, False, "a stage-1-layout remote migrates; a plain clone has no history (§8)"),
    Scenario("switch-off", switch_off, False, "retrieval off: no brief, find says so, capture goes on (§9)"),
    Scenario("stats", stats, False, "ctx stats agrees with what the sessions did (§11)"),
]

MANUAL = [
    "A brief appears at the start of a session in the VS Code extension, in a repository with earlier sessions on the branch (§5).",
    "A VS Code conversation closed without ending is listed as finalized after the idle window, on the next ctx call or commit (§7).",
    "The agent runs `ctx find` before starting on a topic, prompted by the CLAUDE.md block and the ctx skill (§6).",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", action="append", metavar="NAME", help="run only this scenario (repeatable)")
    parser.add_argument("--no-claude", action="store_true", help="skip scenarios that call claude -p")
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory")
    parser.add_argument("--model", help="model for headless sessions (default: Claude Code's default)")
    parser.add_argument("--max-budget-usd", type=float, default=1.0, help="API spend cap per claude -p call")
    parser.add_argument("--plugin-root", help="plugin root to run ctx from (default: detected)")
    parser.add_argument("--plugin-data", help="plugin data directory (default: ~/.claude/plugins/data/wscontext-wscontext)")
    args = parser.parse_args(argv)
    names = {s.name for s in SCENARIOS}
    unknown = set(args.only or []) - names
    if unknown:
        parser.error(f"unknown scenario(s): {', '.join(sorted(unknown))}; --list shows them")
    chosen = [s for s in SCENARIOS if (not args.only or s.name in args.only) and not (args.no_claude and s.claude)]
    if args.list:
        for s in SCENARIOS:
            print(f"{s.name:14} {'claude -p' if s.claude else 'offline  '}  {s.what}")
        print("\nmanual checklist:", *MANUAL, sep="\n  - ")
        return 0

    plugin = locate_plugin(args.plugin_root, args.plugin_data)
    print(f"plugin root: {plugin.root}\nplugin version: {plugin.version}\nplugin data: {plugin.data}")
    for tool in ("git", "uv") + (() if args.no_claude else ("claude",)):
        if shutil.which(tool) is None:
            sys.exit(f"{tool} is not on PATH")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true"}
    tmp = Path(tempfile.mkdtemp(prefix="wscontext-acceptance2-")).resolve()
    print(f"working directory: {tmp}\n")
    acc = Acc(tmp, plugin, args.model, args.max_budget_usd, env)
    Path(acc.commit).write_text(COMMIT_SCRIPT)
    Path(acc.commit).chmod(0o755)
    outcomes: list[tuple[str, str]] = []
    manual = list(MANUAL)
    try:
        for s in chosen:
            started = time.monotonic()
            try:
                status, observed = "PASS", s.run(acc)
            except Fail as e:
                status, observed = "FAIL", str(e)
            except Manual as e:
                status, observed = "MANUAL", str(e)
                manual.insert(0, f"{s.name}: {e}")
            except Exception as e:  # a broken scenario is a failure, not a crash
                status, observed = "FAIL", f"{type(e).__name__}: {e}"
            outcomes.append((s.name, status))
            print(f"{status:6} {s.name}  {observed}  ({time.monotonic() - started:.0f}s)", flush=True)
        acc.plugin.reconcile()
        print(f"\nran against plugin root {acc.plugin.root}, version {acc.plugin.version}")
        print("\nmanual checklist (not driven by this script):", *manual, sep="\n  - ")
    finally:
        if args.keep:
            print(f"\nkept {tmp}")
        else:
            time.sleep(1)  # let detached workers finish writing
            shutil.rmtree(tmp, ignore_errors=True)
    failed = [n for n, st in outcomes if st == "FAIL"]
    print(f"\n{len(outcomes) - len(failed)} of {len(outcomes)} scenarios passed or went manual; failed: {', '.join(failed) or 'none'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
