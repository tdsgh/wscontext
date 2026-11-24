"""Import-stage acceptance run: the capabilities of docs/hist/import-phase-1.md §4 to §10 against the installed plugin.

    .venv/bin/python acceptance/import_past.py [--only NAME ...] [--no-claude] [--list] [--keep]

Not part of the offline suite (pytest never collects `acceptance/`). One scenario drives a real
session through headless Claude Code (`claude -p`) and spends API usage; `--no-claude` runs only
the ones that do not. The others drive the real `ctx` commands, real Git and the real hook
entrypoint with hand-written hook inputs and transcripts, as stages 1 to 3 do. Every repository,
bare remote, clone and worktree lives under one temporary directory; `ctx init`, `ctx import` and
`ctx sync` only ever target paths inside it.

Every `ctx`, Git and hook call runs with `CLAUDE_CONFIG_DIR=<tmp>/claude-home`, so the operator's
real Claude Code folder is never read and the transcripts a scenario writes are the only ones
`ctx import` can see. The `claude -p` call itself does not get that variable (it would lose its
login); see `agent_asks`.

It reuses the plugin lookup, the headless-session driver and the throwaway-repository helpers of
`stage1.py`, `stage2.py` and `stage3.py`, which sit beside it, and runs `ctx` through the installed
plugin's own launcher. It does not run those scripts; the stage gate runs them separately, unchanged.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import tomllib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from record_corpus import isolation
from stage1 import COMMIT_SCRIPT, PLUGIN_ID, Acc, Fail, check, init_repo, locate_plugin, wait_for
from stage2 import (HISTORY_REF, hook, remote_refs, set_config, shared_repo, stats_data,
                    synthetic_session, unique_word)
from stage3 import commit_all, write

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "eval" / "stage2"
CLAUDE_VERSION = "2.1.0"  # a transcript version the adapter supports


class ImportAcc(Acc):
    """Acc whose every subprocess sees a throwaway Claude Code folder (D1).

    Acc.run serves git, ctx and the hook entrypoint; the headless `claude` call is started by
    Popen with `self.env` and so keeps the operator's own folder, which it needs to log in."""

    @property
    def home(self) -> Path:
        return self.tmp / "claude-home"

    def run(self, cmd, cwd, stdin=None, timeout=120, env=None):
        return super().run(cmd, cwd, stdin=stdin, timeout=timeout,
                           env={"CLAUDE_CONFIG_DIR": str(self.home), **(env or {})})


# --- helpers -----------------------------------------------------------------------------------

def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def transcript(acc: ImportAcc, cwd: Path, sid: str, prompt: str, *, commit: tuple[str, str] | None = None,
               command: str | None = None, version: str = CLAUDE_VERSION, age_days: float = 3,
               age_minutes: float = 0, branch: str = "main", folder: str | None = None,
               first_day: datetime | None = None) -> Path:
    """A hand-written Claude Code transcript of one prompt, for a session that ran in `cwd`.

    `commit` is (sha, subject): the session ran `git commit` and Git printed its summary line.
    `command` is another Bash command the session ran. The file is dated `age_days` (and
    `age_minutes`) ago, so it is idle unless a scenario asks for a recent one."""
    modified = datetime.now(timezone.utc) - timedelta(days=age_days, minutes=age_minutes)
    at = first_day or modified - timedelta(hours=1)
    lines: list[dict] = []

    def add(kind: str, message: dict) -> None:
        nonlocal at
        lines.append({"type": kind, "uuid": f"u{len(lines)}", "sessionId": sid, "cwd": str(cwd),
                      "gitBranch": branch, "version": version, "timestamp": iso(at), "message": message})
        at += timedelta(minutes=1)

    def bash(cmd: str, result: str) -> None:
        tid = f"t{len(lines)}"
        add("assistant", {"id": f"m{len(lines)}", "role": "assistant",
                          "content": [{"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": cmd}}]})
        add("user", {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": result}]})

    add("user", {"role": "user", "content": prompt})
    if command:
        bash(command, "ok")
    if commit:
        sha, subject = commit
        bash(f'git commit -m "{subject}"', f"[{branch} {sha[:7]}] {subject}\n 1 file changed, 1 insertion(+)")
    add("assistant", {"id": f"m{len(lines)}", "role": "assistant", "content": [{"type": "text", "text": "done"}],
                      "usage": {"input_tokens": 10, "output_tokens": 5}})
    directory = acc.home / "projects" / (folder or re.sub(r"[^A-Za-z0-9]", "-", str(cwd)))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sid}.jsonl"
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    stamp = modified.timestamp()
    os.utime(path, (stamp, stamp))
    return path


def new_sid() -> str:
    return str(uuid.uuid4())


def real_commit(acc: Acc, repo: Path, rel: str, text: str, subject: str) -> tuple[str, str]:
    write(repo, rel, text)
    return commit_all(acc, repo, subject), subject


def preview(acc: Acc, repo: Path, *flags: str) -> dict:
    return acc.ctx_json(repo, "import", *flags)


def blocks(doc: dict, kind: str) -> list[dict]:
    return [r for r in doc["results"] if r["type"] == kind]


def offered_ids(doc: dict) -> list[str]:
    return [r["session_id"] for r in blocks(doc, "session")]


def number_of(doc: dict, *, sid: str | None = None, path: str | None = None) -> int:
    for r in doc["results"]:
        if (sid and r.get("session_id") == sid) or (path and r.get("path") == path and r["type"] == "document"):
            return r["number"]
    raise Fail(f"{sid or path} is not in the preview: {[r['id'] for r in doc['results']]}")


def do_import(acc: Acc, repo: Path, doc: dict, *flags: str, declined: bool = False, code: int = 0) -> str:
    done = acc.ctx(repo, "import", *(["--declined"] if declined else []), "--offer", doc["token"], *flags, code=code)
    return done.stdout


def records(acc: Acc, repo: Path) -> dict[str, dict]:
    out = {}
    for path in sorted((acc.store(repo) / "records" / "sessions").glob("*.json")):
        out[path.stem] = json.loads(path.read_text())
    return out


def checkpoints(acc: Acc, repo: Path) -> dict[str, list[str]]:
    folder = acc.store(repo) / "records" / "checkpoints"
    return {p.stem: sorted(json.loads(p.read_text())["session_ids"]) for p in sorted(folder.glob("*.json"))}


def unlinked(acc: Acc, repo: Path) -> int:
    linked = set(checkpoints(acc, repo))
    return len([sha for sha in acc.git(repo, "rev-list", "HEAD").split() if sha not in linked])


def snapshot(store: Path, skip: tuple[str, ...] = ("logs/ctx-calls.jsonl",)) -> dict[str, str]:
    seen = {}
    for path in sorted(store.rglob("*")):
        rel = path.relative_to(store).as_posix()
        if path.is_file() and rel not in skip:
            seen[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return seen


def history_tip(acc: Acc, repo: Path) -> str:
    return acc.git(repo, "rev-parse", "--verify", "-q", HISTORY_REF, ok=False)


def call_lines(acc: Acc, repo: Path) -> list[dict]:
    path = acc.store(repo) / "logs" / "ctx-calls.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def import_repo(acc: Acc, name: str, sessions: int = 2, docs: bool = False) -> tuple[Path, list[str]]:
    """An initialised repository, `sessions` idle past transcripts that each made a commit, and
    optionally two documents."""
    repo = acc.repo(name)
    if docs:
        write(repo, "docs/guide.md", "# Guide\n\nHow to run it.\n")
        write(repo, "notes/todo.md", "# Todo\n\nThings later.\n")
        commit_all(acc, repo, "documents")
    init_repo(acc, repo)
    time.sleep(1)  # let the detached workers init starts finish
    sids = []
    for n in range(sessions):
        sid = new_sid()
        made = real_commit(acc, repo, f"src{n}.py", f"VALUE = {n}\n", f"add src{n}")
        transcript(acc, repo, sid, f"build number {n} {unique_word()}", commit=made, age_days=3 + n)
        sids.append(sid)
    return repo, sids


# --- scenarios ---------------------------------------------------------------------------------

def preview_writes_nothing(acc: ImportAcc) -> str:
    repo, sids = import_repo(acc, "preview", docs=True)
    store = acc.store(repo)
    time.sleep(1)
    before, tip, config = snapshot(store), history_tip(acc, repo), (repo / ".wscontext" / "config.toml").read_bytes()
    calls = len(call_lines(acc, repo))
    text = acc.ctx(repo, "import").stdout
    check("RETRIEVED DATA" in text and text.count("[session ") == 2 and "[document " in text,
          f"the preview lacks the frame, two sessions or documents: {text[:500]}")
    for n in (1, 2):
        check(f"[session {n}]" in text, f"no [session {n}] block: {text[:500]}")
    check(re.search(r"--offer [0-9a-f]{8} --all", text), "the preview prints no offer token")
    after = snapshot(store)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    check(not changed, f"the preview changed store files: {changed}")
    check(history_tip(acc, repo) == tip, "the preview moved the history ref")
    check((repo / ".wscontext" / "config.toml").read_bytes() == config, "the preview changed config.toml")
    lines = call_lines(acc, repo)
    check(len(lines) == calls + 1 and lines[-1]["argv"][-1] == "import",
          f"ctx-calls went from {calls} to {len(lines)} lines; last argv {lines[-1]['argv'] if lines else None}")
    check(not records(acc, repo), "a session record exists after a preview")
    return f"2 sessions and {len(blocks(preview(acc, repo), 'document'))} documents previewed; {len(before)} store files, history ref and config.toml unchanged; one ctx-calls line"


def corpus_links(acc: ImportAcc) -> str:
    repo = acc.tmp / "corpus"
    acc.git(acc.tmp, "clone", "-q", str(CORPUS / "repo.bundle"), str(repo))
    acc.identify(repo)
    recorded = {p.stem: sorted(json.loads(p.read_text())["session_ids"])
                for p in (CORPUS / "corpus" / "records" / "checkpoints").glob("*.json")}
    sources = sorted((CORPUS / "corpus" / "raw" / "sessions").glob("*.jsonl"))
    check(len(sources) == 25 and recorded, f"the recorded corpus has {len(sources)} transcripts, {len(recorded)} checkpoints")
    # The recording's commits carry `AI-Session` trailers, and D7 never links such a commit. The
    # recovery case is a commit without one, so the clone's history is rewritten without the
    # trailers; the SHAs change, and the transcripts' summary lines and the recorded links follow.
    mapping = acc.tmp / "corpus-sha-map"
    drop = f"new=$(sed '/^AI-Session:/d' | git commit-tree \"$@\"); echo \"$GIT_COMMIT $new\" >> {mapping}; echo $new"
    acc.run(["git", "filter-branch", "-f", "--commit-filter", drop, "--", "--all"], repo, timeout=300,
            env={"FILTER_BRANCH_SQUELCH_WARNING": "1"})
    for ref in acc.git(repo, "for-each-ref", "--format=%(refname)", "refs/original").split():
        acc.git(repo, "update-ref", "-d", ref)
    moved = dict(line.split() for line in mapping.read_text().splitlines())
    check(len(moved) >= 28 and "AI-Session" not in acc.git(repo, "log", "--all", "--format=%B"),
          f"the clone still has trailers or lost commits ({len(moved)} mapped)")
    recorded = {moved[k]: v for k, v in recorded.items()}
    folder = acc.home / "projects" / "corpus"
    folder.mkdir(parents=True)
    old = time.time() - 3 * 86400
    wrapped = 0
    for source in sources:
        sid = re.fullmatch(r"\d{4}-\d\d-\d\d-(.+)\.jsonl", source.name)[1]
        out = []
        for line in source.read_text().splitlines():
            for was, now_sha in moved.items():
                line = line.replace(was[:7], now_sha[:7])
            obj = json.loads(line)
            if "cwd" in obj:
                obj["cwd"] = str(repo)
            for block in (obj.get("message") or {}).get("content") or []:
                given = block.get("input") if isinstance(block, dict) and block.get("type") == "tool_use" else None
                if given and "acc-commit" in given.get("command", ""):
                    # the recording's commit wrapper is `git commit -m "$1"`; D7 recognises only `git commit`
                    given["command"] = re.sub(r"^\S*acc-commit ", "git commit -m ", given["command"])
                    wrapped += 1
            out.append(json.dumps(obj))
        target = folder / f"{sid}.jsonl"
        target.write_text("\n".join(out) + "\n")
        os.utime(target, (old, old))
    init_repo(acc, repo)
    time.sleep(1)
    doc = preview(acc, repo)
    check(len(offered_ids(doc)) == 25, f"{len(offered_ids(doc))} of 25 corpus sessions are offered; unreadable: {len(blocks(doc, 'unreadable'))}")
    now, after = doc["unlinked_now"], doc["unlinked_after"]
    check(now == unlinked(acc, repo), f"the preview says {now} unlinked commits now, rev-list minus checkpoints says {unlinked(acc, repo)}")
    out = do_import(acc, repo, doc, "--sessions")
    check(out.startswith("Imported 25 sessions"), f"import summary: {out[:200]}")
    got = checkpoints(acc, repo)
    missing = {k: v for k, v in recorded.items() if got.get(k) != v}
    extra = {k: v for k, v in got.items() if k not in recorded}
    check(not missing and not extra, f"links differ from the recorded corpus: {len(missing)} missing or different "
          f"{dict(list(missing.items())[:2])}, {len(extra)} extra {dict(list(extra.items())[:2])}")
    check(unlinked(acc, repo) == after, f"unlinked commits after the import: {unlinked(acc, repo)}, the preview said {after}")
    check(f"{len(recorded)} commit" in out or f"linked {len(recorded)} commit" in out, f"summary names {len(recorded)} links: {out[:200]}")
    return f"25 sessions imported ({wrapped} wrapper commits written as `git commit`, trailers removed from the clone), {len(recorded)} checkpoint links equal the recorded ones; unlinked commits {now} before, {after} after (as printed)"


def choose_and_decline(acc: ImportAcc) -> str:
    repo, sids = import_repo(acc, "choose", sessions=4, docs=True)
    doc = preview(acc, repo)
    check(len(offered_ids(doc)) == 4, f"offered sessions: {offered_ids(doc)}")
    docs = [r["path"] for r in blocks(doc, "document")]
    check(docs.index("docs/guide.md") < docs.index("notes/todo.md"), f"Suggested does not come before Other: {docs}")
    mine = [r["session_id"] for r in blocks(doc, "session")]  # newest first
    taken = [number_of(doc, sid=mine[0]), number_of(doc, sid=mine[1]), number_of(doc, path="docs/guide.md")]
    drop = number_of(doc, sid=mine[2])
    out = do_import(acc, repo, doc, "--take", ",".join(map(str, sorted(taken))), "--decline", str(drop))
    check(out.startswith("Imported 2 sessions") and "added 1 document." in out and "Declined 1 item; it is not offered again." in out,
          f"summary: {out[:300]}")
    check(set(records(acc, repo)) == set(mine[:2]), f"records after the import: {sorted(records(acc, repo))}")
    again = preview(acc, repo)
    check(offered_ids(again) == [mine[3]], f"a rerun offers {offered_ids(again)}, wanted only {mine[3]}")
    check("docs/guide.md" not in [r["path"] for r in blocks(again, "document")], "a taken document is offered again")
    both = acc.ctx(repo, "import", "--declined").stdout
    check("declined earlier" in both and mine[2] in both, f"--declined does not list the declined session: {both[:400]}")
    last = blocks(preview(acc, repo, "--declined"), "session")[-1]
    check(last["session_id"] == mine[2] and last["declined"], f"the declined session is not last and marked: {last}")
    do_import(acc, repo, again, "--all")
    check(preview_text(acc, repo) == "Nothing new to import.", f"after --all: {preview_text(acc, repo)!r}")
    check(mine[2] not in records(acc, repo), "the declined session was imported")
    return "two sessions and a document taken, one session declined; rerun offered the rest, --declined listed the declined one, then nothing new"


def preview_text(acc: Acc, repo: Path) -> str:
    return acc.ctx(repo, "import").stdout.strip()


def folders(acc: ImportAcc) -> str:
    repo, _ = import_repo(acc, "folders", sessions=0)
    sub = repo / "sub"
    sub.mkdir()
    ids = {name: new_sid() for name in ("root", "sub", "worktree", "gone")}
    inside = acc.tmp / "folders-wt"
    gone = acc.tmp / "folders-gone"
    acc.git(repo, "worktree", "add", "-q", "-b", "other", str(inside))
    acc.git(repo, "worktree", "add", "-q", "-b", "temp", str(gone))
    transcript(acc, repo, ids["root"], "work at the root")
    transcript(acc, sub, ids["sub"], "work in the subfolder", age_days=4)
    transcript(acc, inside, ids["worktree"], "work in a worktree", age_days=5, branch="other")
    transcript(acc, gone, ids["gone"], "work in a worktree that was removed", age_days=6, branch="temp")
    acc.git(repo, "worktree", "remove", "--force", str(gone))
    check(not gone.exists(), "the worktree folder is still there")
    doc = preview(acc, repo)
    got = {r["session_id"]: r for r in blocks(doc, "session")}
    check(ids["gone"] not in got, "the session of a removed outside worktree is offered")
    check({ids["root"], ids["sub"], ids["worktree"]} <= set(got), f"offered {sorted(got)}")
    check(not got[ids["root"]]["folder"], f"a session at the root shows a folder: {got[ids['root']]['folder']!r}")
    check("sub" in (got[ids["sub"]]["folder"] or ""), f"subfolder session folder: {got[ids['sub']]['folder']!r}")
    check("folders-wt" in (got[ids["worktree"]]["folder"] or ""), f"worktree session folder: {got[ids['worktree']]['folder']!r}")
    text = acc.ctx(repo, "import").stdout
    check("folder: " in text, "the text preview prints no folder line")
    return f"root, subfolder ({got[ids['sub']]['folder']}) and outside worktree ({got[ids['worktree']]['folder']}) offered; the removed worktree's session is not"


def held_and_in_use(acc: ImportAcc) -> str:
    repo, _ = import_repo(acc, "held", sessions=0)
    live, recent, idle = new_sid(), new_sid(), new_sid()
    synthetic_session(acc, repo, live)
    transcript(acc, repo, live, "captured live")
    transcript(acc, repo, recent, "touched a minute ago", age_days=0, age_minutes=1)
    transcript(acc, repo, idle, "idle for days")
    doc = preview(acc, repo)
    check(offered_ids(doc) == [idle], f"offered {offered_ids(doc)}, wanted only the idle one")
    check(doc["in_use"] == 1, f"in use: {doc['in_use']}")
    text = acc.ctx(repo, "import").stdout
    check("1 session in use right now was left out." in text, f"the preview does not count the session in use: {text[-600:]}")
    check(live not in text and recent not in text, "a held or in-use session appears in the preview")
    return "the live-captured session is held, the one touched a minute ago is counted as in use, the idle one is offered"


def unreadable(acc: ImportAcc) -> str:
    repo, _ = import_repo(acc, "unreadable", sessions=0)
    good, odd = new_sid(), new_sid()
    transcript(acc, repo, good, "a readable one")
    transcript(acc, repo, odd, "from a future version", version="99.0.0", first_day=datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc))
    doc = preview(acc, repo)
    found = blocks(doc, "unreadable")
    check(len(found) == 1 and found[0]["id"] == f"{odd}.jsonl", f"unreadable blocks: {[b['id'] for b in found]}")
    check(found[0]["date"] == "2026-09-02" and "number" not in found[0], f"date or number on the unreadable one: {found[0]}")
    check(offered_ids(doc) == [good], f"offered {offered_ids(doc)}")
    text = acc.ctx(repo, "import").stdout
    check(f"[unreadable {odd}.jsonl] 2026-09-02 cannot be imported" in text, f"text block: {text[:600]}")
    check(do_import(acc, repo, doc, "--all").startswith("Imported 1 session,"), "the readable session was not imported")
    check(odd not in records(acc, repo), "the unreadable transcript was imported")
    return "a transcript of an unknown version is listed with its date and no number, and is never imported"


def show_and_why(acc: ImportAcc) -> str:
    repo, _ = import_repo(acc, "showwhy", sessions=0)
    word = unique_word()
    sid = new_sid()
    made = real_commit(acc, repo, "pkg/widget.py", "def widget_zq():\n    return 1\n", "add widget")
    transcript(acc, repo, sid, f"add the widget {word}", commit=made)
    do_import(acc, repo, preview(acc, repo), "--all")
    found = next((s for s in acc.ctx_json(repo, "sessions", "--full")["results"] if s["id"] == sid), None)
    check(found and found.get("imported_at"), f"sessions --full lacks imported_at for the session: {found}")
    check("imported on" in acc.ctx(repo, "sessions").stdout, "ctx sessions does not mark the session")
    shown = acc.ctx(repo, "show", sid).stdout
    check("imported on" in shown and word in shown, f"ctx show: {shown[:500]}")
    hit = [h for h in acc.ctx_json(repo, "find", word)["results"] if h["type"] == "session" and h["id"] == sid]
    check(hit and any("imported session" in line for line in hit[0]["body"]), f"ctx find hit: {hit}")
    why = acc.ctx(repo, "why", "pkg/widget.py:1").stdout
    check(sid[:8] in why and "imported" in why, f"ctx why: {why[:600]}")
    impact = acc.ctx_json(repo, "impact", "pkg/widget.py#widget_zq")
    check(any(sid in str(r) for r in impact["results"] if r["type"] == "session"),
          f"ctx impact does not list the session: {[r['id'] for r in impact['results']]}")
    return f"session {sid[:8]} carries its imported mark in sessions, show, find and why, and is listed by impact"


def documents(acc: ImportAcc) -> str:
    repo, _ = import_repo(acc, "documents", sessions=1)
    first, second = unique_word(), unique_word()
    write(repo, "docs/architecture.md", f"# Architecture\n\nThe ledger {first} is append-only.\n\n## Storage\n\nFiles on disk.\n")
    write(repo, "notes/todo.md", "# Todo\n\nLater.\n")
    write(repo, "CLAUDE.md", f"# Rules\n\nNever mention {second}.\n")
    commit_all(acc, repo, "documents")
    doc = preview(acc, repo)
    group = {r["path"]: r["group"] for r in blocks(doc, "document")}
    check(group.get("docs/architecture.md") == "suggested" and group.get("notes/todo.md") == "other", f"groups: {group}")
    check("CLAUDE.md" not in group, f"CLAUDE.md is offered: {sorted(group)}")
    text = acc.ctx(repo, "import").stdout
    check("Suggested" in text and "Other" in text, "the text preview names no group")
    out = do_import(acc, repo, doc, "--documents")
    check("added 3 documents." in out or "document" in out, f"summary: {out[:200]}")
    listed = (repo / ".wscontext" / "config.toml").read_text()
    check("[documents]" in listed and "docs/architecture.md" in listed and "CLAUDE.md" not in listed, f"config.toml: {listed[-300:]}")
    hits = acc.ctx_json(repo, "find", "--kind", "document", first)["results"]
    check(hits and hits[0]["type"] == "document" and hits[0]["id"].startswith("docs/architecture.md:"),
          f"find --kind document: {[(h['type'], h['id']) for h in hits]}")
    check(hits[0]["links"] and hits[0]["links"][0].startswith("docs/architecture.md:"), f"no link to the section: {hits[0]['links']}")
    check(not acc.ctx_json(repo, "find", "--kind", "document", second)["results"], "CLAUDE.md is searchable")
    third = unique_word()
    write(repo, "docs/architecture.md", (repo / "docs/architecture.md").read_text() + f"\n## Later\n\nAdded {third}.\n")
    check(acc.ctx_json(repo, "find", "--kind", "document", third)["results"], "an edited document is not found on the next call")
    set_config(repo, "retrieval", "enabled", "false")
    off = acc.ctx(repo, "find", "--kind", "document", first, code=None)
    check("switched off" in off.stdout + off.stderr, f"retrieval off: {(off.stdout + off.stderr)[:300]}")
    return "Suggested and Other groups, CLAUDE.md left out, a section found with its link, an edit found on the next call, retrieval off switches search off"


def sync_shared(acc: ImportAcc) -> str:
    bare, a = shared_repo(acc, "sync-a")
    time.sleep(1)
    first, second = new_sid(), new_sid()
    for sid, n in ((first, 0), (second, 1)):
        made = real_commit(acc, a, f"s{n}.py", f"X = {n}\n", f"add s{n}")
        transcript(acc, a, sid, f"shared work {n}", commit=made, age_days=3 + n)
    out = do_import(acc, a, preview(acc, a), "--sessions")
    check(out.startswith("Imported 2 sessions, linked 2 commits"), f"clone A import: {out[:200]}")
    check("git push" in out, f"no sync note after the import: {out}")
    acc.git(a, "push", "-q", "origin", "main")
    wait_for(lambda: HISTORY_REF in remote_refs(acc, a), f"the pre-push hook to push {HISTORY_REF}")
    b = acc.tmp / "sync-b"
    acc.git(acc.tmp, "clone", "-q", str(bare), str(b))
    acc.identify(b)
    local = new_sid()
    for sid in (first, second):  # the same operator's transcripts, as they sit on a second machine
        transcript(acc, b, sid, "shared work", age_days=3)
    transcript(acc, b, local, "work only here", age_days=2)
    acc.ctx(b, "init")
    wait_for(lambda: {first, second} <= {s["id"] for s in acc.ctx_json(b, "sessions")["results"]},
             "clone B to fetch the imported sessions", 60)
    doc = preview(acc, b)
    check(offered_ids(doc) == [local], f"clone B is offered {offered_ids(doc)}, wanted only its own {local}")
    return "clone A imported 2 sessions and pushed; clone B fetched them and is offered only its own transcript"


def stats_apart(acc: ImportAcc) -> str:
    repo, _ = import_repo(acc, "stats", sessions=0)
    caller = new_sid()
    hook(acc, repo, "SessionStart", caller, source="startup")
    hook(acc, repo, "UserPromptSubmit", caller, prompt="find things")
    hook(acc, repo, "PostToolUse", caller, tool_name="Bash", tool_input={"command": "ctx code helper"}, tool_response={"stdout": "ok"})
    hook(acc, repo, "Stop", caller)
    hook(acc, repo, "SessionEnd", caller)
    acc.wait_ended(repo, caller)
    wait_for(lambda: acc.ctx_json(repo, "stats", "session", caller)["results"][0]["ctx_calls"], "the live session's ctx calls")
    before = acc.ctx_json(repo, "stats", "queries")["results"][0]["adoption"]
    count = stats_data(acc, repo)["sessions"]
    check(before["sessions"] == 1 and before["with_code"] == 1, f"adoption before: {before}")
    for n in range(2):  # these two ran `ctx code` and `ctx impact`; counted, they would change adoption
        transcript(acc, repo, new_sid(), f"imported work {n}", command="ctx code helper && ctx impact helper", age_days=3 + n)
    do_import(acc, repo, preview(acc, repo), "--all")
    data = stats_data(acc, repo)
    check(data["sessions"] == count + 2 and data["imported_sessions"] == 2, f"stats data: {data}")
    check("(2 imported)" in acc.ctx(repo, "stats", "data").stdout, "the text of stats data does not show the imported count")
    after = acc.ctx_json(repo, "stats", "queries")["results"][0]["adoption"]
    check(after == before, f"adoption changed: before {before}, after {after}")
    return f"stats data shows {data['sessions']} sessions, 2 imported; adoption unchanged: {after}"


def init_line(acc: ImportAcc) -> str:
    quiet = acc.repo("init-quiet")
    (quiet / "README.md").unlink()
    acc.git(quiet, "commit", "-q", "-am", "no documents")
    out = acc.ctx(quiet, "init").stdout
    check("Past work found" not in out, f"init with nothing to find printed the line: {out[-300:]}")
    repo = acc.repo("init-line")
    sid = new_sid()
    transcript(acc, repo, sid, "work before ctx was set up")
    out = acc.ctx(repo, "init").stdout
    line = "Past work found: 1 session, 1 document. Run `ctx import` to see it."  # README.md is the document
    check(line in out, f"init prints no singular found line for one session and one document: {out[-400:]}")
    check(out.rstrip().splitlines()[-1] == line, "the found line is not the last line of init")
    check(not records(acc, repo), "init wrote a session record")
    check(not (acc.store(repo) / "raw" / "sessions").exists() or not list((acc.store(repo) / "raw" / "sessions").iterdir()),
          "init copied a transcript")
    check(offered_ids(preview(acc, repo)) == [sid], "the session is not offered after init")
    return f"init printed {line!r} as its last line and imported nothing; none when nothing is found"


def agent_asks(acc: ImportAcc) -> str:
    # The claude call keeps the operator's CLAUDE_CONFIG_DIR (it needs its login), so the `ctx`
    # it runs reads the real transcript folder. Nothing is placed there: the repository's
    # documents give the preview numbered items, and the real folder holds no transcript whose
    # cwd is this throwaway repository.
    repo, _ = import_repo(acc, "agent", sessions=0, docs=True)
    done = acc.claude(repo, "Import the past work of this repository", tools="Read Bash(ctx:*) Bash(git status:*)")
    check(not done.is_error, f"claude reported an error: {done.text[:300]}")
    check(any(t == "Bash" for t in done.tools), f"the session ran no command (tools: {done.tools})")
    check("docs/guide.md" in done.text and re.search(r"\b1\b", done.text), f"the reply shows no numbered items: {done.text[:500]}")
    check("?" in done.text, f"the reply does not ask which to import: {done.text[-300:]}")
    check(not [sid for sid, r in records(acc, repo).items() if r.get("imported_at")], "a record has imported_at")
    # `ctx init` writes an empty `[documents] paths = []`; only a chosen document fills it
    chosen = tomllib.loads((repo / ".wscontext" / "config.toml").read_text()).get("documents", {}).get("paths")
    check(not chosen, f"documents were added to config.toml: {chosen}")
    return f"the session previewed and asked (tools {done.tools}); no record has imported_at"


@dataclass
class Scenario:
    name: str
    run: Callable[[ImportAcc], str]
    claude: bool
    what: str


SCENARIOS = [
    Scenario("preview_writes_nothing", preview_writes_nothing, False, "ctx import changes no store file, ref or config, and logs one call (§7)"),
    Scenario("corpus_links", corpus_links, False, "the 25 corpus transcripts import with the recorded checkpoint links (§5, §10)"),
    Scenario("choose_and_decline", choose_and_decline, False, "--take, --decline, --declined, reruns and nothing new (§7, §8)"),
    Scenario("folders", folders, False, "subfolder and outside worktree offered with their folder; a removed worktree is not (§4)"),
    Scenario("held_and_in_use", held_and_in_use, False, "a live-captured session is held; a recent transcript is in use (§4)"),
    Scenario("unreadable", unreadable, False, "an unknown version is listed with its date, unnumbered (§4)"),
    Scenario("show_and_why", show_and_why, False, "sessions, show, find, why and impact show the imported session and its mark (§4, §5)"),
    Scenario("documents", documents, False, "groups, CLAUDE.md left out, find --kind document, edits, retrieval off (§6)"),
    Scenario("sync_shared", sync_shared, False, "clone B is not offered what clone A imported and pushed (§4, §9)"),
    Scenario("stats_apart", stats_apart, False, "stats data counts imported sessions; adoption is unchanged (§9)"),
    Scenario("init_line", init_line, False, "ctx init prints the found line and imports nothing (§7)"),
    Scenario("agent_asks", agent_asks, True, "a headless session previews the import and asks (§7)"),
]

MANUAL = [
    "In VS Code, ask the agent to import past work in a repository with real history: it shows the numbered list and waits for a choice (§7).",
    "The `ctx` skill's section on importing reads correctly against the real preview (§7).",
    "The person's part of the gate: document ranking in a fresh clone of this repository (build plan WP7 gate, step 5).",
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
            print(f"{s.name:24} {'claude -p' if s.claude else 'offline  '}  {s.what}")
        print("\nmanual checklist:", *MANUAL, sep="\n  - ")
        return 0

    plugin = locate_plugin(args.plugin_root, args.plugin_data)
    print(f"plugin root: {plugin.root}\nplugin version: {plugin.version}\nplugin data: {plugin.data}")
    for tool in ("git", "uv") + (() if args.no_claude or not any(s.claude for s in chosen) else ("claude",)):
        if shutil.which(tool) is None:
            sys.exit(f"{tool} is not on PATH")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true"}
    env.pop("CLAUDE_CONFIG_DIR", None)  # ImportAcc.run sets it per call; the claude call gets none
    tmp = Path(tempfile.mkdtemp(prefix="wscontext-acceptance-import-")).resolve()
    print(f"working directory: {tmp}\n")
    acc = ImportAcc(tmp, plugin, args.model, args.max_budget_usd, env, extra=isolation(PLUGIN_ID))
    acc.home.mkdir()
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
    print(f"\n{len(outcomes) - len(failed)} of {len(outcomes)} scenarios passed; failed: {', '.join(failed) or 'none'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
