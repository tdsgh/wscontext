"""Stage-3 acceptance run: the capabilities of docs/wscontext-phase-3.md §4 to §13 against the installed plugin.

    .venv/bin/python acceptance/stage3.py [--only NAME ...] [--no-claude] [--list] [--keep]

Not part of the offline suite (pytest never collects `acceptance/`). One scenario drives a real
session through headless Claude Code (`claude -p`) and spends API usage; `--no-claude` runs only the
ones that do not. The others drive the real `ctx` commands, real Git and the real hook entrypoint
with hand-written hook inputs and transcripts, as stages 1 and 2 do. Every repository lives under
one temporary directory; `ctx init` and `ctx rebuild` only ever target paths inside it.

It reuses the plugin lookup, the headless-session driver and the throwaway-repository helpers of
`stage1.py` and `stage2.py`, which sit beside it, and runs `ctx` through the installed plugin's own
launcher. It does not run those two scripts; the stage gate runs them separately, unchanged.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from record_corpus import isolation
from stage1 import COMMIT_SCRIPT, PLUGIN_ID, Acc, Fail, check, init_repo, locate_plugin, wait_for
from stage2 import (doctor_item, hook, set_config, stats_data, stats_session, synthetic_session,
                    unique_word)

TRIGGER_CODE = "ctx code"
TRIGGER_IMPACT = "ctx impact"
SMALL_BUDGET_MS = 300  # the graph budget the freshness scenario sets, so a first call is incomplete
LARGE_FILES = 3600


# --- helpers -----------------------------------------------------------------------------------

def write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def commit_all(acc: Acc, repo: Path, message: str) -> str:
    acc.git(repo, "add", "-A")
    acc.git(repo, "commit", "-q", "-m", message)
    return acc.git(repo, "rev-parse", "HEAD")


def results(doc: dict, kind: str | None = None) -> list[dict]:
    return [r for r in doc["results"] if kind is None or r["type"] == kind]


def definitions(acc: Acc, repo: Path, target: str) -> list[dict]:
    return results(acc.ctx_json(repo, "code", target), "definition")


def where(found: list[dict]) -> list[str]:
    return [f"{d['path']}:{d['start']}" for d in found]


def code_complete(doc: dict) -> bool:
    return not set(doc["flags"]) & {"graph-incomplete", "graph-busy"}


def python_repo(acc: Acc, name: str) -> Path:
    """A small Python project with several commits that change core.py and use.py together."""
    repo = acc.repo(name)
    write(repo, "pkg/__init__.py", "")
    write(repo, "pkg/core.py",
          "class Ledger:\n"
          "    def total(self):\n"
          "        return helper()\n"
          "\n"
          "    def settle(self):\n"
          "        return use_scale(2)\n"
          "\n"
          "\n"
          "def helper():\n"
          "    return 1\n"
          "\n"
          "\n"
          "def helper_two():\n"
          "    return helper() + 1\n")
    write(repo, "pkg/use.py",
          "from pkg.core import helper\n"
          "from pkg.scale_a import scale\n"
          "\n"
          "\n"
          "def go():\n"
          "    return helper()\n"
          "\n"
          "\n"
          "def use_scale(n):\n"
          "    return scale(n)\n"
          "\n"
          "\n"
          "def top():\n"
          "    return go()\n")
    write(repo, "pkg/scale_a.py", "def scale(n):\n    return n * 2\n")
    write(repo, "pkg/scale_b.py", "def scale(n):\n    return n * 3\n")
    write(repo, "tests/test_core.py",
          "from pkg.core import helper\n"
          "\n"
          "\n"
          "def test_helper():\n"
          "    assert helper() == 1\n")
    write(repo, "tools.rb", "def ruby_only\n  1\nend\n")
    write(repo, ".gitignore", "ignored/\n")
    write(repo, "ignored/ghost.py", "def ghost_zq():\n    return 0\n")
    commit_all(acc, repo, "start")
    for n in (2, 3):
        write(repo, "pkg/core.py", (repo / "pkg/core.py").read_text() + f"\n# revision {n}\n")
        write(repo, "pkg/use.py", (repo / "pkg/use.py").read_text() + f"\n# revision {n}\n")
        commit_all(acc, repo, f"revision {n}")
    init_repo(acc, repo)
    return repo


def session_commit(acc: Acc, repo: Path, sid: str, rel: str, text: str, message: str) -> str:
    """A synthetic session that writes one file and makes a plain `git commit`; returns the SHA, linked."""
    hook(acc, repo, "SessionStart", sid, source="startup")
    hook(acc, repo, "UserPromptSubmit", sid, prompt=f"change {rel}")
    write(repo, rel, text)
    sha = commit_all(acc, repo, message)
    check(acc.trailers(repo) == [sid], f"commit trailers {acc.trailers(repo)}, wanted {[sid]}")
    acc.checkpoint(repo, sha)
    hook(acc, repo, "Stop", sid)
    hook(acc, repo, "SessionEnd", sid)
    acc.wait_ended(repo, sid)
    return sha


# --- scenarios ---------------------------------------------------------------------------------

def code_by_name(acc: Acc) -> str:
    repo = python_repo(acc, "code-name")
    found = definitions(acc, repo, "helper")
    check(len(found) >= 2 and found[0]["name"] == "helper" and found[0]["path"] == "pkg/core.py",
          f"exact match is not first: {where(found)}")
    check("helper_two" in [d["name"] for d in found[1:]], f"names containing the text are missing: {where(found)}")
    first = found[0]
    check((first["kind"], first["start"], first["end"], first["signature"]) == ("function", 9, 10, "def helper()"),
          f"helper's kind, lines or first line: {first}")
    method = definitions(acc, repo, "total")[0]
    check(method["kind"] == "method" and method["qualname"] == "Ledger.total" and method["parent"] == "Ledger",
          f"a method does not name its class: {method}")
    text = acc.ctx(repo, "code", "helper").stdout
    check("RETRIEVED DATA" in text and "function helper" in text and "ctx impact pkg/core.py#helper" in text,
          f"text answer lacks the frame, the kind or the follow-up command: {text[:400]}")
    limited = acc.ctx_json(repo, "--limit", "1", "code", "helper")
    check(len(limited["results"]) == 1 and limited["omitted"] >= 1, "--limit 1 did not cut the answer")
    check(acc.ctx_json(repo, "--full", "code", "helper")["results"], "--full printed nothing")
    check(definitions(acc, repo, "ghost_zq") == [], "a definition in a file Git ignores was found")
    gone = acc.ctx(repo, "code", "ignored/ghost.py").stdout
    check("not in the graph" in gone, f"an ignored file is not reported as outside the graph: {gone[:200]}")
    other = acc.ctx(repo, "code", "tools.rb", code=0).stdout
    check("not in the graph" in other, f"an unsupported language is not reported as outside the graph: {other[:200]}")
    no = acc.ctx(repo, "code", "no_such_name_zq", code=None)
    check("RETRIEVED DATA" in no.stdout or no.returncode != 0, "an unknown name printed nothing")
    return "exact match first, then names containing the text; kind, lines, first line and class; limit; ignored and unsupported files stay out"


def code_outline(acc: Acc) -> str:
    repo = python_repo(acc, "code-file")
    doc = acc.ctx_json(repo, "code", "pkg/core.py")
    head = results(doc, "file")
    check(len(head) == 1 and head[0]["language"] == "python" and head[0]["status"] == "ok", f"file block: {head}")
    check("pkg/use.py" in str(head[0]["imported_by"]) and "tests/test_core.py" in str(head[0]["imported_by"]),
          f"importers of core.py: {head[0]['imported_by']}")
    outline = results(doc, "definition")
    check([d["qualname"] for d in outline] == ["Ledger", "Ledger.total", "Ledger.settle", "helper", "helper_two"],
          f"outline order: {[d['qualname'] for d in outline]}")
    check([d["start"] for d in outline] == sorted(d["start"] for d in outline), "outline is not in file order")
    imports = results(acc.ctx_json(repo, "code", "pkg/use.py"), "file")[0]["imports"]
    check("pkg/core.py" in str(imports), f"use.py does not list the file it imports: {imports}")
    classes = results(acc.ctx_json(repo, "code", "--kind", "class", "pkg/core.py"), "definition")
    check([d["qualname"] for d in classes] == ["Ledger"], f"--kind class on an outline: {[d['qualname'] for d in classes]}")
    return "outline in file order with lines; imports and importers; --kind filters the outline"


def code_working_tree(acc: Acc) -> str:
    repo = python_repo(acc, "code-tree")
    acc.ctx(repo, "code", "helper")  # the graph exists and is current
    before = definitions(acc, repo, "helper")[0]["start"]
    # edits that are not committed: an added function, a new untracked file, lines inserted above
    path = repo / "pkg" / "core.py"
    path.write_text("# one\n# two\n# three\n" + path.read_text() + "\n\ndef fresh_zq():\n    return 2\n")
    write(repo, "pkg/brand_new.py", "def untracked_zq():\n    return 3\n")
    check(acc.git(repo, "status", "--porcelain"), "the edits are not pending in the working tree")
    added = definitions(acc, repo, "fresh_zq")
    check(len(added) == 1 and added[0]["path"] == "pkg/core.py", f"an uncommitted function is not found: {where(added)}")
    check(where(definitions(acc, repo, "untracked_zq")) == ["pkg/brand_new.py:1"], "an untracked file is not found")
    moved = definitions(acc, repo, "helper")[0]["start"]
    check(moved == before + 3, f"helper's lines follow the file as it is now: was {before}, now {moved}, wanted {before + 3}")
    path.unlink()
    check(definitions(acc, repo, "fresh_zq") == [] and not [d for d in definitions(acc, repo, "helper") if d["path"] == "pkg/core.py"],
          "a deleted file still has definitions")
    return "uncommitted edits, an untracked file, moved lines and a deleted file show at once"


def code_unparsable(acc: Acc) -> str:
    repo = python_repo(acc, "code-bad")
    write(repo, "pkg/bad.py", "def broken(:\n    pass\n\n\ndef fine_zq():\n    return 1\n")
    doc = acc.ctx_json(repo, "code", "pkg/bad.py")
    check("parse-errors" in doc["flags"] and "pkg/bad.py" in doc["note"], f"flags {doc['flags']}, note {doc.get('note')!r}")
    block = results(doc, "file")[0]
    check(block["status"] == "error" and block["error"], f"file block: status {block['status']}, error {block['error']!r}")
    check("fine_zq" in [d["name"] for d in results(doc, "definition")], "the definitions the parser did find were dropped")
    shown = acc.ctx(repo, "code", "fine_zq").stdout
    check("parse-errors" in shown, f"the text answer does not carry the flag: {shown[-300:]}")
    check(acc.ctx_json(repo, "stats", "code")["results"][0]["graph"]["by_status"]["error"] >= 1,
          "stats code counts no file that could not be parsed")
    write(repo, "pkg/bad.py", "def broken():\n    pass\n")
    check("parse-errors" not in acc.ctx_json(repo, "code", "broken")["flags"], "the flag stayed after the file was fixed")
    return "a file that does not parse is flagged by name, keeps the definitions found, and the flag clears when fixed"


def rebuild(acc: Acc) -> str:
    repo = python_repo(acc, "rebuild")
    before = where(definitions(acc, repo, "helper"))
    graph = acc.store(repo) / "index" / "graph.db"
    check(graph.exists(), "no graph.db after the first answer")
    for suffix in ("", "-wal", "-shm"):
        Path(f"{graph}{suffix}").unlink(missing_ok=True)
    check(where(definitions(acc, repo, "helper")) == before, "answers differ after the graph was deleted")
    out = acc.ctx(repo, "rebuild", "--graph").stdout
    check("graph" in out.lower(), f"rebuild --graph printed {out[:200]!r}")
    check(where(definitions(acc, repo, "helper")) == before, "answers differ after ctx rebuild --graph")
    both = acc.ctx_json(repo, "rebuild")
    check("graph" in both, f"rebuild --json lacks the graph object: {sorted(both)}")
    done = acc.ctx(repo, "rebuild", "--index", "--graph", code=None)
    check(done.returncode != 0, "--index and --graph together were accepted")
    return "deleting graph.db and ctx rebuild --graph both give the same answers"


def impact(acc: Acc) -> str:
    repo = python_repo(acc, "impact")
    doc = acc.ctx_json(repo, "impact", "helper")
    check(doc["counts"]["callers"] >= 2 and doc["counts"]["tests"] >= 1 and doc["counts"]["importers"] >= 1,
          f"counts: {doc['counts']}")
    target = results(doc, "target")[0]
    check((target["path"], target["qualname"]) == ("pkg/core.py", "helper"), f"target: {target['path']}#{target['qualname']}")
    callers = results(doc, "caller")
    check({c["qualname"] for c in callers} >= {"Ledger.total", "helper_two", "go"}, f"callers: {[c['qualname'] for c in callers]}")
    check(all(c["match"] == "name" and "name match" in c["body"][0] and c["links"] for c in callers),
          "a caller is not marked as a name match with a command to open it")
    check(all(c["path"].startswith("tests/") for c in results(doc, "test") if c.get("kind") == "caller"), "a non-test listed as a test")
    check("tests/test_core.py" in [t["path"] for t in results(doc, "test")], "the test is not listed separately")
    check("pkg/use.py" in [i["path"] for i in results(doc, "importer")], "the importer of core.py is missing")
    check("tests/test_core.py" not in [c["path"] for c in callers], "a test is also listed among the callers")
    check("name only" in doc["note"] or "by name only" in doc["note"], f"the answer does not say calls are matched by name: {doc['note']!r}")
    shared = acc.ctx_json(repo, "impact", "pkg/scale_a.py#scale")
    check(all(c["shared"] == 2 and "2 definitions share this name" in c["body"][0] for c in results(shared, "caller")) and results(shared, "caller"),
          f"a shared name does not report how many definitions share it: {[c['body'] for c in results(shared, 'caller')]}")
    pairs = results(acc.ctx_json(repo, "impact", "pkg/core.py#helper"), "cochange")
    check(any(p["path"] == "pkg/use.py" and p["count"] >= 2 and 0 < p["share"] <= 1 for p in pairs),
          f"files that change together: {[(p['path'], p['count']) for p in pairs]}")
    check("RETRIEVED DATA" in acc.ctx(repo, "impact", "helper").stdout, "text answer lacks the frame")
    limited = acc.ctx_json(repo, "--limit", "1", "impact", "helper")
    check(len(results(limited, "caller")) == 1 and limited["omitted"] >= 1, "--limit 1 did not cut the callers")
    return "callers marked as name matches with their share count, tests and importers listed apart, co-change counts, limit"


def impact_depth_and_targets(acc: Acc) -> str:
    repo = python_repo(acc, "impact-depth")
    one = {c["qualname"] for c in results(acc.ctx_json(repo, "impact", "pkg/core.py#helper"), "caller")}
    two = {c["qualname"] for c in results(acc.ctx_json(repo, "impact", "--depth", "2", "pkg/core.py#helper"), "caller")}
    check(one < two and "Ledger.total" in one, f"depth 1 {sorted(one)}, depth 2 {sorted(two)}: depth 2 adds nothing")
    ambiguous = acc.ctx(repo, "impact", "scale", code=1)
    check("Several definitions match" in ambiguous.stdout and "pkg/scale_a.py" in ambiguous.stdout and "pkg/scale_b.py" in ambiguous.stdout,
          f"ambiguous target: {ambiguous.stdout[:300]}")
    unknown = acc.ctx(repo, "impact", "no_such_name_zq", code=1)
    check("no definition matches" in unknown.stderr + unknown.stdout, f"unknown target: {(unknown.stderr + unknown.stdout)[:200]}")
    path = acc.ctx_json(repo, "impact", "pkg/core.py")
    check(results(path, "target")[0]["id"] == "pkg/core.py" and results(path, "importer"), "impact on a file lists no importers")
    return "depth follows callers; an ambiguous target lists candidates (exit 1); an unknown one is an error (exit 1); a file works"


def impact_sessions(acc: Acc) -> str:
    repo = python_repo(acc, "impact-sessions")
    sid = "imp-" + uuid.uuid4().hex[:8]
    session_commit(acc, repo, sid, "pkg/core.py",
                   (repo / "pkg/core.py").read_text().replace("def helper():\n    return 1", "def helper():\n    return 10"),
                   "raise helper")
    sessions = results(acc.ctx_json(repo, "impact", "pkg/core.py#helper"), "session")
    check(any(sid in str(s) for s in sessions), f"the session that changed helper is not listed: {[s['id'] for s in sessions]}")
    text = acc.ctx(repo, "impact", "pkg/core.py#helper").stdout
    check(f"ctx show {sid}" in text, "the answer carries no command that opens the session")
    other = results(acc.ctx_json(repo, "impact", "pkg/core.py#Ledger.settle"), "session")
    check(not any(sid in str(s) for s in other), "a session is linked to a function it did not change")
    # a session that never committed is linked to its files only
    quiet = "imp-quiet-" + uuid.uuid4().hex[:6]
    hook(acc, repo, "SessionStart", quiet, source="startup")
    hook(acc, repo, "UserPromptSubmit", quiet, prompt="look at scale")
    hook(acc, repo, "PostToolUse", quiet, tool_name="Edit", tool_input={"file_path": str(repo / "pkg" / "scale_a.py")},
         tool_response={"ok": True})
    hook(acc, repo, "Stop", quiet)
    hook(acc, repo, "SessionEnd", quiet)
    acc.wait_ended(repo, quiet)
    file_sessions = results(acc.ctx_json(repo, "impact", "pkg/scale_a.py"), "session")
    check(any(quiet in str(s) for s in file_sessions), f"a session with no commit is not listed on its file: {file_sessions}")
    return f"session {sid[:8]} listed on the function it changed, not on another; a session without a commit appears on its file"


def why_name(acc: Acc) -> str:
    repo = python_repo(acc, "why-name")
    sid = "why-" + uuid.uuid4().hex[:8]
    session_commit(acc, repo, sid, "pkg/core.py",
                   (repo / "pkg/core.py").read_text().replace("def helper():\n    return 1", "def helper():\n    return 11"),
                   "raise helper again")
    out = acc.ctx(repo, "why", "pkg/core.py#Ledger.total").stdout
    check("RETRIEVED DATA" in out and "commit" in out, f"why on Class.method: {out[:300]}")
    one = acc.ctx_json(repo, "why", "#helper_two")
    check(not one.get("ambiguous") and one["results"], f"why #name on a unique name: {one}")
    path = repo / "pkg" / "core.py"
    # the session's lines move: a commit by hand inserts lines above, then an edit that is not committed
    path.write_text("# a\n# b\n# c\n" + path.read_text())
    commit_all(acc, repo, "lines moved")
    moved = acc.ctx(repo, "why", "pkg/core.py#helper").stdout
    check(sid in moved, f"the session is lost after its lines moved: {moved[:400]}")
    path.write_text("# d\n" + path.read_text())
    uncommitted = acc.ctx_json(repo, "why", "pkg/core.py#helper")
    check(any(sid in str(r) for r in uncommitted["results"]), "the session is lost with an uncommitted edit above the function")
    ambiguous = acc.ctx(repo, "why", "#scale", code=1)
    check("Several definitions match" in ambiguous.stdout and "pkg/scale_a.py" in ambiguous.stdout and "pkg/scale_b.py" in ambiguous.stdout,
          f"ambiguous name: {ambiguous.stdout[:300]}")
    check(acc.ctx_json(repo, "why", "--limit", "1", "pkg/core.py#helper")["results"], "--limit made why empty")
    unknown = acc.ctx(repo, "why", "#no_such_name_zq", code=1)
    check("no definition" in unknown.stderr + unknown.stdout, f"unknown name: {(unknown.stderr + unknown.stdout)[:200]}")
    line = acc.ctx(repo, "why", "pkg/core.py:12", code=None)
    check(line.returncode == 0, "why FILE:LINE stopped working")
    return f"why finds Class.method and #name through the graph, keeps session {sid[:8]} after lines moved, lists candidates for an ambiguous name"


def freshness_branch(acc: Acc) -> str:
    repo = python_repo(acc, "branch")
    acc.git(repo, "switch", "-q", "-c", "feature")
    write(repo, "pkg/feature.py", "def on_feature_zq():\n    return 1\n")
    commit_all(acc, repo, "feature work")
    acc.git(repo, "switch", "-q", "main")
    acc.ctx(repo, "code", "helper")  # builds graph.db, the sign that a graph exists here
    files = lambda: acc.ctx_json(repo, "stats", "code")["results"][0]["graph"]["by_language"]["python"]["files"]
    on_main = files()
    started = time.monotonic()
    acc.git(repo, "switch", "-q", "feature")
    took = time.monotonic() - started
    check(took < 10, f"the branch switch took {took:.1f}s")
    # `ctx stats code` never refreshes, so only the background worker can bring the new file in
    wait_for(lambda: files() == on_main + 1, "the graph to follow the branch switch in the background", 60)
    doc = acc.ctx_json(repo, "code", "on_feature_zq")
    check(where(results(doc, "definition")) == ["pkg/feature.py:1"] and code_complete(doc),
          f"first call after a switch: {where(results(doc, 'definition'))}, flags {doc['flags']}")
    acc.git(repo, "switch", "-q", "main")
    wait_for(lambda: files() == on_main, "the graph to follow the switch back", 60)
    back = acc.ctx_json(repo, "code", "on_feature_zq")
    check(not back["results"] and code_complete(back), f"the other branch's file is still answered: {back}")
    return f"switch took {took:.1f}s; the graph followed it in the background and the next call was complete, both ways"


def freshness_large(acc: Acc) -> str:
    repo = acc.repo("large")
    for d in range(LARGE_FILES // 100):
        for f in range(100):
            body = "".join(f"def fn_{d}_{f}_{k}(x):\n    return fn_{d}_{f}_{(k + 1) % 8}(x) + {k}\n\n\n" for k in range(8))
            write(repo, f"m{d}/f{f}.py", body)
    commit_all(acc, repo, "generated")
    init_repo(acc, repo)
    set_config(repo, "graph", "query_budget_ms", str(SMALL_BUDGET_MS))
    started = time.monotonic()
    first = acc.ctx_json(repo, "code", "fn_3_4_5")
    took = time.monotonic() - started
    check("graph-incomplete" in first["flags"], f"a first call on {LARGE_FILES} files was not flagged incomplete (flags {first['flags']}, {took:.1f}s)")
    check(took < SMALL_BUDGET_MS / 1000 + 5, f"the first call took {took:.1f}s with a {SMALL_BUDGET_MS} ms budget")
    check("still being built" in first["note"], f"the flag has no explanation: {first['note']!r}")
    final = wait_for(lambda: (d := acc.ctx_json(repo, "code", "fn_3_4_5")) and code_complete(d) and d,
                     "a later call to be complete", 120)
    check(where(results(final, "definition")) == ["m3/f4.py:21"], f"the complete answer: {where(results(final, 'definition'))}")
    files = acc.ctx_json(repo, "stats", "code")["results"][0]["graph"]["by_language"]["python"]["files"]
    check(files == LARGE_FILES, f"the graph holds {files} of {LARGE_FILES} files")
    return f"first call on {LARGE_FILES} files returned in {took:.1f}s flagged graph-incomplete; a later call was complete"


TS_REPO = {
    "src/model.ts": ("export interface Shape {\n  area(): number;\n}\n\nexport type Id = string;\n\n"
                     "export enum Color { Red, Blue }\n\nexport class Circle implements Shape {\n"
                     "  constructor(private r: number) {}\n  area(): number {\n    return scale(this.r);\n  }\n}\n\n"
                     "export function scale(n: number): number {\n  return n * 2;\n}\n"),
    "src/app.ts": 'import { Circle, scale } from "./model";\n\nexport function run_ts(): number {\n  return new Circle(2).area() + scale(1);\n}\n',
    "src/util.js": "function scale(n) {\n  return n * 3;\n}\nmodule.exports = { scale };\n",
    "src/main.js": 'const { scale } = require("./util");\n\nfunction start_js() {\n  return scale(4);\n}\nmodule.exports = { start_js };\n',
    "py/mod.py": "def scale(n):\n    return n\n\n\ndef only_py(x):\n    return scale(x)\n",
}


def languages(acc: Acc) -> str:
    repo = acc.repo("langs")
    for rel, text in TS_REPO.items():
        write(repo, rel, text)
    commit_all(acc, repo, "three languages")
    init_repo(acc, repo)
    shape = definitions(acc, repo, "Shape")
    check(len(shape) == 1 and shape[0]["kind"] == "type" and shape[0]["path"] == "src/model.ts", f"TypeScript interface: {shape}")
    kinds = {d["name"]: d["kind"] for d in definitions(acc, repo, "src/model.ts")}
    check(kinds == {"Shape": "type", "Id": "type", "Color": "type", "Circle": "class", "constructor": "method",
                    "area": "method", "scale": "function"}, f"TypeScript outline kinds: {kinds}")
    check("src/app.ts" in str(results(acc.ctx_json(repo, "code", "src/model.ts"), "file")[0]["imported_by"]), "a TypeScript import is not resolved")
    check("src/main.js" in str(results(acc.ctx_json(repo, "code", "src/util.js"), "file")[0]["imported_by"]), "a require() is not resolved")
    callers = {c["qualname"] for c in results(acc.ctx_json(repo, "impact", "src/util.js#scale"), "caller")}
    check("start_js" in callers, f"a JavaScript caller: {callers}")
    by_language = acc.ctx_json(repo, "stats", "code")["results"][0]["graph"]["by_language"]
    check(set(by_language) == {"python", "typescript", "javascript"} and by_language["typescript"]["files"] == 2
          and by_language["javascript"]["files"] == 2 and by_language["python"]["files"] == 1, f"stats by language: {by_language}")
    # answers for the languages that stay on, before the switch
    keep = {"js outline": acc.ctx_json(repo, "code", "src/main.js")["results"],
            "js impact": acc.ctx_json(repo, "impact", "src/main.js#start_js")["results"],
            "py outline": acc.ctx_json(repo, "code", "py/mod.py")["results"],
            "py impact": acc.ctx_json(repo, "impact", "py/mod.py#only_py")["results"]}
    set_config(repo, "graph", "languages", '["python", "javascript"]')
    gone = acc.ctx_json(repo, "code", "src/model.ts")
    check(not gone["results"] and "not in the graph" in gone["note"], f"TypeScript answers after switching it off: {gone}")
    check(not definitions(acc, repo, "Shape") and not definitions(acc, repo, "run_ts"), "TypeScript definitions remain after switching it off")
    after = {"js outline": acc.ctx_json(repo, "code", "src/main.js")["results"],
             "js impact": acc.ctx_json(repo, "impact", "src/main.js#start_js")["results"],
             "py outline": acc.ctx_json(repo, "code", "py/mod.py")["results"],
             "py impact": acc.ctx_json(repo, "impact", "py/mod.py#only_py")["results"]}
    for name in keep:
        check(keep[name] == after[name], f"{name} changed when TypeScript was switched off")
    stats = acc.ctx_json(repo, "stats", "code")["results"][0]
    check("typescript" not in stats["graph"]["by_language"] and stats["languages"]["enabled"] == ["python", "javascript"], f"stats after: {stats['languages']}")
    check(doctor_item(acc, repo, "language typescript")["detail"] == "off", "doctor does not show TypeScript as off")
    cochange_only = acc.ctx_json(repo, "impact", "src/app.ts", code=None)
    check("flags" in cochange_only, "impact on a switched-off language's file printed no document")
    set_config(repo, "graph", "languages", '["python", "typescript", "javascript"]')
    check(len(definitions(acc, repo, "Shape")) == 1, "TypeScript did not return when switched on again")
    return "TypeScript kinds, imports and callers; JavaScript require; switching TypeScript off changes only its answers, and on again restores them"


def retrieval_off(acc: Acc) -> str:
    repo = python_repo(acc, "off")
    block = (repo / "CLAUDE.md").read_text()
    check(TRIGGER_CODE in block and TRIGGER_IMPACT in block and "ctx find" in block, f"the block lacks a trigger with retrieval on: {block!r}")
    check(len(block.split("<!-- wscontext:begin -->")[1].split("<!-- wscontext:end -->")[0]) < 900, "the block grew into a manual")
    check(doctor_item(acc, repo, "claude.md block")["status"] == "ok", "doctor flags a fresh block")
    on = acc.ctx_json(repo, "code", "helper")["results"]
    set_config(repo, "retrieval", "enabled", "false")
    for command in ("code", "impact"):
        out = acc.ctx(repo, command, "helper", code=0).stdout
        check("switched off" in out, f"ctx {command} with retrieval off: {out[:200]}")
        doc = acc.ctx_json(repo, command, "helper")
        check(doc["enabled"] is False and not doc.get("results"), f"ctx --json {command} with retrieval off: {doc}")
    check(acc.ctx(repo, "why", "pkg/core.py#helper").stdout.count("RETRIEVED DATA") >= 1, "why stopped working with retrieval off")
    check(acc.ctx_json(repo, "why", "#helper_two")["results"], "why #name stopped working on the graph with retrieval off")
    check(acc.ctx_json(repo, "stats", "code")["results"][0]["graph"]["by_language"], "stats code stopped with retrieval off")
    stale = doctor_item(acc, repo, "claude.md block")
    check(stale["status"] == "warn", f"doctor does not flag a block whose triggers disagree with the switch: {stale}")
    acc.ctx(repo, "init")
    text = (repo / "CLAUDE.md").read_text()
    check(TRIGGER_CODE not in text and TRIGGER_IMPACT not in text and "ctx find" not in text and "ctx why" in text,
          f"the block after init with retrieval off: {text!r}")
    check(doctor_item(acc, repo, "claude.md block")["status"] == "ok", "doctor flags the block after re-init")
    set_config(repo, "retrieval", "enabled", "true")
    acc.ctx(repo, "init")
    text = (repo / "CLAUDE.md").read_text()
    check(TRIGGER_CODE in text and TRIGGER_IMPACT in text, "init did not restore the graph triggers")
    check(acc.ctx_json(repo, "code", "helper")["results"] == on, "answers differ after switching retrieval back on")
    return "ctx code and ctx impact answer 'switched off', why and stats go on, the block loses and regains its triggers"


def search_tool_names(acc: Acc) -> str:
    repo = acc.repo("toolnames")
    init_repo(acc, repo)
    sid, word = "names-" + uuid.uuid4().hex[:8], unique_word()
    transcript = acc.tmp / f"{sid}.jsonl"
    lines = [
        {"type": "user", "version": "2.1.0", "sessionId": sid, "uuid": "p1", "timestamp": "2026-11-11T10:00:00Z",
         "message": {"role": "user", "content": "tidy the ledger"}},
        {"type": "assistant", "version": "2.1.0", "sessionId": sid, "uuid": "r1", "timestamp": "2026-11-11T10:00:02Z",
         "message": {"id": "m1", "role": "assistant", "content": [
             {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": f"{word} --check"}}]},
         "wireToolInputs": {"t1": {"command": f"{word} --check"}}},
        {"type": "user", "version": "2.1.0", "sessionId": sid, "uuid": "tr1", "timestamp": "2026-11-11T10:00:03Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "fine"}]},
         "toolUseResult": {"stdout": "fine"}},
        {"type": "assistant", "version": "2.1.0", "sessionId": sid, "uuid": "r2", "timestamp": "2026-11-11T10:00:05Z",
         "message": {"id": "m2", "role": "assistant", "content": [{"type": "text", "text": "done"}]}},
    ]
    transcript.write_text("".join(json.dumps(line) + "\n" for line in lines))
    extra = {"transcript_path": str(transcript)}
    hook(acc, repo, "SessionStart", sid, source="startup", **extra)
    hook(acc, repo, "UserPromptSubmit", sid, prompt="tidy the ledger", **extra)
    hook(acc, repo, "Stop", sid, **extra)
    hook(acc, repo, "SessionEnd", sid, **extra)
    acc.wait_ended(repo, sid)

    def found(*words: str) -> bool:
        return any(h["type"] == "session" and h["id"] == sid for h in acc.ctx_json(repo, "find", *words)["results"])

    check(wait_for(lambda: found(word), "the tool's input text to be searchable", 30), "the text a tool call was given is not searchable")
    for name in ("bash", "Bash"):
        check(not found(name), f"calling the {name} tool makes the session match the word {name!r}")
    return "the text a Bash call was given finds the session; the tool's name does not"


def stats_code(acc: Acc) -> str:
    repo = python_repo(acc, "stats-code")
    for _ in range(2):
        acc.ctx(repo, "code", "helper")
    acc.ctx(repo, "impact", "helper")
    time.sleep(2.5)  # a file read in the last two seconds is compared by hash until a later refresh
    acc.ctx(repo, "code", "helper")
    listed = acc.git(repo, "ls-files", "-co", "--exclude-standard", "*.py").splitlines()
    outlines = sum(len(results(acc.ctx_json(repo, "code", f), "definition")) for f in listed)
    graph = acc.ctx_json(repo, "stats", "code")["results"][0]
    py = graph["graph"]["by_language"]["python"]
    check(py["files"] == len(listed) and py["symbols"] == outlines, f"stats {py} vs {len(listed)} files and {outlines} definitions in the outlines")
    check(sum(graph["graph"]["by_status"].values()) == len(listed), f"by status {graph['graph']['by_status']} vs {len(listed)} files")
    check(graph["graph"]["last_build"] and graph["graph"]["bytes"] > 0 and graph["changed_files"] == 0, f"build, size or changes: {graph}")
    check(graph["languages"]["enabled"] == ["python", "typescript", "javascript"] and not graph["languages"]["unavailable"], f"languages: {graph['languages']}")
    # the health check: parser, one line per language, the graph; it never refreshes
    names = {c["check"]: c for c in acc.ctx_json(repo, "doctor", code=None)["checks"]}
    for name in ("code parser", "language python", "language typescript", "language javascript", "code graph"):
        check(name in names and names[name]["status"] == "ok", f"doctor {name}: {names.get(name)}")
    write(repo, "pkg/core.py", (repo / "pkg/core.py").read_text() + "\n\ndef late_zq():\n    return 4\n")
    check(acc.ctx_json(repo, "stats", "code")["results"][0]["changed_files"] == 1, "stats code does not count the edited file")
    check(doctor_item(acc, repo, "code graph")["status"] == "warn", "doctor does not warn about a file changed since the last read")
    time.sleep(2.5)  # a file written in the last two seconds is only rehashed, not yet counted as read
    acc.ctx(repo, "code", "late_zq")
    check(acc.ctx_json(repo, "stats", "code")["results"][0]["changed_files"] == 0, "the refresh did not bring changed files to 0")
    check(doctor_item(acc, repo, "code graph")["status"] == "ok", "doctor still warns after the refresh")
    # adoption: ctx calls a session ran, counted from its hook inputs
    caller, quiet = "adopt-" + uuid.uuid4().hex[:8], "adopt-" + uuid.uuid4().hex[:8]
    hook(acc, repo, "SessionStart", caller, source="startup")
    hook(acc, repo, "UserPromptSubmit", caller, prompt="find things")
    for command in ("ctx code helper", "ctx impact helper", "ctx why #helper", "ctx why pkg/core.py#helper", "ctx why pkg/core.py:1"):
        hook(acc, repo, "PostToolUse", caller, tool_name="Bash", tool_input={"command": command}, tool_response={"stdout": "ok"})
    hook(acc, repo, "Stop", caller)
    hook(acc, repo, "SessionEnd", caller)
    synthetic_session(acc, repo, quiet)
    acc.wait_ended(repo, caller)
    done = wait_for(lambda: (d := stats_session(acc, repo, caller)) and d["ctx_calls"] and d, "the session's ctx calls")
    check(done["ctx_calls"] == {"code": 1, "impact": 1, "why#name": 2, "why": 1}, f"session ctx calls: {done['ctx_calls']}")
    queries = acc.ctx_json(repo, "stats", "queries")["results"][0]
    adoption = queries["adoption"]
    check((adoption["sessions"], adoption["with_code"], adoption["with_impact"], adoption["with_why_name"]) == (2, 1, 1, 1),
          f"adoption: {adoption}")
    check(queries["by_command"].get("code", 0) >= 2 and queries["by_command"].get("impact", 0) >= 1, f"queries by command: {queries['by_command']}")
    check(stats_data(acc, repo)["sessions"] == 2, "stats data lost a session")
    return f"{py['files']} files and {py['symbols']} definitions match the outlines; doctor and changed-files follow an edit; adoption counts code, impact and why on a name"


def adoption(acc: Acc) -> str:
    repo = python_repo(acc, "adoption")
    name = "settle_" + uuid.uuid4().hex[:8]
    write(repo, "pkg/billing.py", f"def {name}(invoice):\n    return invoice\n")
    commit_all(acc, repo, "billing")
    acc.ctx(repo, "code", name)  # the graph is built; the session pays for the answer, not the build
    prompt = f"In this repository, where is `{name}` defined? Answer with the file path and the line number only."
    done = acc.claude(repo, prompt, tools="Read Bash(ctx:*) Bash(git status:*)")
    check(not done.is_error, f"claude reported an error: {done.text[:300]}")
    check("pkg/billing.py" in done.text, f"the answer does not name the file: {done.text[:200]}")
    acc.wait_ended(repo, done.session_id)
    record = wait_for(lambda: (d := stats_session(acc, repo, done.session_id)) and d["ended_by"] and d, "the session record")
    calls = record["ctx_calls"]
    check(calls.get("code", 0) >= 1, f"the session did not call ctx code (tools used: {done.tools}, ctx calls: {calls})")
    adopted = acc.ctx_json(repo, "stats", "queries")["results"][0]["adoption"]
    check(adopted["with_code"] == 1, f"adoption does not count the session: {adopted}")
    return f"asked where {name} is defined, the session ran ctx code {calls['code']} time(s); tools {done.tools}"


@dataclass
class Scenario:
    name: str
    run: Callable[[Acc], str]
    claude: bool
    what: str


SCENARIOS = [
    Scenario("code-by-name", code_by_name, False, "ctx code on a name: exact first, kind, lines, class, limit, ignored and unsupported files (§4)"),
    Scenario("code-outline", code_outline, False, "ctx code on a file: outline, imports, importers, --kind (§4)"),
    Scenario("code-working-tree", code_working_tree, False, "answers match uncommitted edits, untracked and deleted files (§4)"),
    Scenario("code-unparsable", code_unparsable, False, "a file that does not parse is flagged and keeps its definitions (§4)"),
    Scenario("rebuild", rebuild, False, "deleting graph.db or ctx rebuild --graph loses nothing (§4, §7)"),
    Scenario("impact", impact, False, "callers marked as name matches, tests, importers, co-change (§5)"),
    Scenario("impact-depth", impact_depth_and_targets, False, "depth, ambiguous and unknown targets (§5)"),
    Scenario("impact-sessions", impact_sessions, False, "sessions that changed the function, and files-only sessions (§5)"),
    Scenario("why-name", why_name, False, "ctx why on Class.method and #name, sessions after lines moved, ambiguity (§6)"),
    Scenario("freshness-branch", freshness_branch, False, "a branch switch refreshes the graph in the background (§7)"),
    Scenario("freshness-large", freshness_large, False, "a first call on a large repository answers within the budget, flagged incomplete (§7)"),
    Scenario("languages", languages, False, "TypeScript and JavaScript; switching one language off changes only its answers (§8)"),
    Scenario("retrieval-off", retrieval_off, False, "both commands off, block without triggers, why and stats go on (§9, §10)"),
    Scenario("tool-names", search_tool_names, False, "a tool's name no longer makes a session match (§12)"),
    Scenario("stats-code", stats_code, False, "ctx stats code and doctor agree with the run; adoption counts (§13)"),
    Scenario("adoption", adoption, True, "a headless session asked where something is defined calls ctx code (§9, §13)"),
]

MANUAL = [
    "In VS Code, switch branches, then run `ctx code <name>` from the agent or a terminal: the answer carries no `graph-incomplete` (§7).",
    "The agent runs `ctx impact` before changing a function that other code calls, prompted by the CLAUDE.md block and the ctx skill (§9).",
    "The `ctx` skill's text for `ctx code`, `ctx impact` and `ctx why` on a name reads correctly against real output (§9).",
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
            print(f"{s.name:18} {'claude -p' if s.claude else 'offline  '}  {s.what}")
        print("\nmanual checklist:", *MANUAL, sep="\n  - ")
        return 0

    plugin = locate_plugin(args.plugin_root, args.plugin_data)
    print(f"plugin root: {plugin.root}\nplugin version: {plugin.version}\nplugin data: {plugin.data}")
    for tool in ("git", "uv") + (() if args.no_claude or not any(s.claude for s in chosen) else ("claude",)):
        if shutil.which(tool) is None:
            sys.exit(f"{tool} is not on PATH")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true"}
    tmp = Path(tempfile.mkdtemp(prefix="wscontext-acceptance3-")).resolve()
    print(f"working directory: {tmp}\n")
    acc = Acc(tmp, plugin, args.model, args.max_budget_usd, env, extra=isolation(PLUGIN_ID))
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
