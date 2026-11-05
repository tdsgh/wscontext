"""Stage-1 acceptance run: the scenarios of docs/roadmap.md § Stage 1 against the installed plugin.

    .venv/bin/python acceptance/stage1.py [--only NAME ...] [--no-claude] [--list] [--keep]

Not part of the offline suite (pytest never collects `acceptance/`). Scenarios that need a
session drive headless Claude Code (`claude -p`) and spend API usage; `--no-claude` runs only
the ones that do not. Every repository, bare remote and clone lives under one temporary
directory; `ctx init`, `ctx sync` and `git push` only ever target paths inside it.

`ctx` runs through the installed plugin's own launcher (`<plugin root>/bin/ctx`) with the
`--plugin-root`/`--plugin-data` options the hook commands pass. The plugin's `bin/` is on PATH
only inside Claude Code's Bash tool, so a plain shell has no bare `ctx`; this launcher is the
code path the hooks use, and it builds the plugin environment when it is stale.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

PLUGIN_ID = "wscontext@wscontext"
DEFAULT_DATA = Path.home() / ".claude" / "plugins" / "data" / "wscontext-wscontext"
# What a headless session may do: edit files and make commits, nothing else.
TOOLS = "Write Edit Read Bash(git add:*) Bash(git commit:*) Bash(git status:*)"
CLAUDE_TIMEOUT = 300
WAIT = 60


class Fail(Exception):
    pass


class Manual(Exception):
    """The scenario cannot be driven headless; the reason goes to the manual checklist."""


def check(condition: object, message: str) -> None:
    if not condition:
        raise Fail(message)


def wait_for(probe: Callable[[], object], what: str, timeout: float = WAIT):
    end = time.monotonic() + timeout
    while True:
        value = probe()
        if value:
            return value
        if time.monotonic() > end:
            raise Fail(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(0.5)


@dataclass
class Plugin:
    root: Path
    data: Path
    candidates: dict[str, str]

    @property
    def version(self) -> str:
        try:
            return json.loads((self.root / ".claude-plugin" / "plugin.json").read_text())["version"]
        except (OSError, ValueError, KeyError):
            return "unknown"

    @property
    def ctx(self) -> list[str]:
        return [str(self.root / "bin" / "ctx"), "--plugin-root", str(self.root),
                "--plugin-data", str(self.data)]

    def reconcile(self) -> None:
        """Follow the root Claude Code's hooks ran: the launcher records it on every run."""
        try:
            seen = Path((self.data / "plugin-root").read_text())
        except OSError:
            return
        if seen != self.root and (seen / "bin" / "ctx").exists():
            print(f"note: hooks ran plugin root {seen}, not {self.root}; ctx now uses it")
            self.root = seen


def locate_plugin(root: str | None, data: str | None) -> Plugin:
    data_dir = Path(data) if data else DEFAULT_DATA
    found = {}
    listing = subprocess.run(["claude", "plugin", "list", "--json"], capture_output=True, text=True)
    if listing.returncode == 0:
        entry = next((p for p in json.loads(listing.stdout) if p.get("id") == PLUGIN_ID), None)
        if entry:
            found = {"readFromFolder": entry.get("readFromFolder"), "installPath": entry.get("installPath")}
    candidates = {k: v for k, v in found.items() if v and (Path(v) / "bin" / "ctx").exists()}
    if root:
        chosen = Path(root)
    else:
        recorded = None
        try:
            recorded = Path((data_dir / "plugin-root").read_text())
        except OSError:
            pass
        if recorded and (recorded / "bin" / "ctx").exists():
            chosen = recorded
        elif candidates:
            chosen = Path(next(iter(candidates.values())))
        else:
            sys.exit(f"{PLUGIN_ID} is not installed (claude plugin list): install it first, or pass --plugin-root")
    return Plugin(chosen, data_dir, candidates)


@dataclass
class Claude:
    session_id: str
    is_error: bool
    text: str
    tools: list[str] = field(default_factory=list)


@dataclass
class Acc:
    tmp: Path
    plugin: Plugin
    model: str | None
    budget: float
    env: dict[str, str]

    # --- Git and ctx ---

    def run(self, cmd: list[str], cwd: Path, stdin: str | None = None, timeout: float = 120,
            env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, cwd=cwd, input=stdin, capture_output=True, text=True,
                              timeout=timeout, env={**self.env, **(env or {})})

    def git(self, cwd: Path, *args: str, ok: bool = True, env: dict[str, str] | None = None) -> str:
        done = self.run(["git", *args], cwd, env=env)
        if ok:
            check(done.returncode == 0, f"git {' '.join(args)} failed: {done.stderr.strip()}")
        return done.stdout.strip()

    def repo(self, name: str, branch: str = "main") -> Path:
        path = self.tmp / name
        path.mkdir()
        self.git(path, "init", "-q", "-b", branch)
        self.identify(path)
        (path / "README.md").write_text("# throwaway\n")
        self.git(path, "add", "README.md")
        self.git(path, "commit", "-q", "-m", "initial")
        return path

    def identify(self, repo: Path) -> None:
        self.git(repo, "config", "user.name", "Acceptance")
        self.git(repo, "config", "user.email", "acceptance@example.invalid")

    def ctx(self, repo: Path, *args: str, code: int | None = 0) -> subprocess.CompletedProcess:
        done = self.run([*self.plugin.ctx, *args], repo, timeout=300)
        if code is not None:
            check(done.returncode == code,
                  f"ctx {' '.join(args)} exited {done.returncode}, wanted {code}: "
                  f"{(done.stdout + done.stderr).strip()[:400]}")
        return done

    def ctx_json(self, repo: Path, *args: str, code: int | None = 0) -> dict:
        done = self.ctx(repo, "--json", *args, code=code)
        try:
            return json.loads(done.stdout)
        except ValueError:
            raise Fail(f"ctx --json {' '.join(args)} printed no JSON: {(done.stdout + done.stderr)[:300]}")

    def store(self, repo: Path) -> Path:
        common = self.git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        return Path(common) / "wscontext"

    def trailers(self, repo: Path, rev: str = "HEAD") -> list[str]:
        out = self.git(repo, "log", "-1", "--format=%(trailers:key=AI-Session,valueonly)", rev)
        return [line.strip() for line in out.splitlines() if line.strip()]

    def checkpoint(self, repo: Path, sha: str) -> Path:
        return wait_for(lambda: (p := self.store(repo) / "records" / "checkpoints" / f"{sha}.json").exists() and p,
                        f"checkpoint for {sha[:12]}")

    def session(self, repo: Path, sid: str) -> dict | None:
        found = self.ctx_json(repo, "sessions", "--full")["results"]
        return next((s for s in found if s["id"] == sid), None)

    def wait_ended(self, repo: Path, sid: str) -> dict:
        return wait_for(lambda: (s := self.session(repo, sid)) and s["ended_at"] and s,
                        f"session {sid} to end in the store")

    # --- synthetic sessions: the real hook entrypoint, no model call ---

    def hook(self, repo: Path, event: str, sid: str) -> None:
        payload = json.dumps({"session_id": sid, "cwd": str(repo), "transcript_path": "",
                              "hook_event_name": event})
        self.run([*self.plugin.ctx, "hook", event], repo, stdin=payload, timeout=60)

    # --- headless Claude Code ---

    def claude_cmd(self, session: str, resume: bool, tools: str) -> list[str]:
        cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose",
               "--permission-mode", "dontAsk", "--allowedTools", tools,
               "--max-budget-usd", str(self.budget),
               "--resume" if resume else "--session-id", session]
        return cmd + (["--model", self.model] if self.model else [])

    def claude_start(self, repo: Path, prompt: str, session: str | None = None, resume: bool = False,
                     tools: str = TOOLS) -> tuple[str, subprocess.Popen]:
        sid = session or str(uuid.uuid4())
        proc = subprocess.Popen(self.claude_cmd(sid, resume, tools), cwd=repo, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env)
        proc.stdin.write(prompt)
        proc.stdin.close()
        return sid, proc

    def claude_finish(self, sid: str, proc: subprocess.Popen) -> Claude:
        try:
            out, err = proc.communicate(timeout=CLAUDE_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise Fail(f"claude -p ran longer than {CLAUDE_TIMEOUT}s")
        tools, result = [], {}
        for line in out.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "assistant" and not event.get("parent_tool_use_id"):
                tools += [b.get("name", "?") for b in event["message"].get("content", [])
                          if isinstance(b, dict) and b.get("type") == "tool_use"]
            elif event.get("type") == "result":
                result = event
        check(result, f"claude -p produced no result (exit {proc.returncode}): {err.strip()[:300]}")
        self.plugin.reconcile()
        return Claude(result.get("session_id", sid), bool(result.get("is_error")),
                      str(result.get("result", "")), tools)

    def claude(self, repo: Path, prompt: str, **kw) -> Claude:
        sid, proc = self.claude_start(repo, prompt, **kw)
        return self.claude_finish(sid, proc)

    def commit_session(self, repo: Path, name: str, text: str, message: str, marker: str = "",
                       **kw) -> Claude:
        """A session that writes one file and commits it; returns once the commit is linked."""
        prompt = (f"{marker} Create the file {name} containing exactly this text: {text}\n"
                  f"Then run `git add {name}`, and then `git commit -m \"{message}\"`, as two "
                  "separate commands. Do nothing else; reply done.").strip()
        done = self.claude(repo, prompt, **kw)
        check(not done.is_error, f"claude reported an error: {done.text[:300]}")
        check(self.git(repo, "log", "-1", "--format=%s") == message,
              f"the session made no commit {message!r}; tools used: {done.tools}")
        return done


def init_repo(acc: Acc, repo: Path) -> None:
    acc.ctx(repo, "init")


# --- scenarios ---------------------------------------------------------------------------------

def setup(acc: Acc) -> str:
    repo = acc.repo("setup")
    hooks = repo / ".git" / "hooks"
    (hooks / "post-commit").write_text(f"#!/bin/sh\necho ran >> '{repo}/chained.marker'\n")
    (hooks / "post-commit").chmod(0o755)
    (repo / "CLAUDE.md").write_text("# mine\n")
    out = acc.ctx(repo, "init").stdout
    check("post-commit" in out and "chained" in out, f"init did not report chaining: {out[:300]}")
    check((hooks / "post-commit.pre-wscontext").exists(), "original post-commit hook not parked")
    acc.ctx(repo, "init")
    check((repo / "CLAUDE.md").read_text().count("<!-- wscontext:begin -->") == 1,
          "second init duplicated the CLAUDE.md block")
    sid = "setup-" + uuid.uuid4().hex[:8]
    acc.hook(repo, "SessionStart", sid)  # gives doctor a hook run to report
    doctor = acc.ctx_json(repo, "doctor")
    bad = [f"{c['check']}={c['status']}: {c['detail']}" for c in doctor["checks"] if c["status"] != "ok"]
    check(not bad, f"doctor is not all ok: {bad}")
    acc.git(repo, "commit", "--allow-empty", "-q", "-m", "after init")
    wait_for((repo / "chained.marker").exists, "the chained post-commit hook to run", 10)
    acc.hook(repo, "SessionEnd", sid)
    store = acc.store(repo)
    out = acc.ctx(repo, "uninstall").stdout
    check(not (hooks / "prepare-commit-msg").exists(), "prepare-commit-msg shim left behind")
    check((hooks / "post-commit").read_text().count("echo ran") == 1, "original post-commit hook not restored")
    text = (repo / "CLAUDE.md").read_text()
    check("wscontext" not in text and "# mine" in text, f"CLAUDE.md after uninstall: {text!r}")
    check((store / "records" / "sessions" / f"{sid}.json").exists(), "captured data removed by uninstall")
    return "init chained post-commit, doctor all ok, uninstall restored hooks and kept the session record"


def capture_resume(acc: Acc) -> str:
    repo = acc.repo("resume")
    init_repo(acc, repo)
    first = acc.claude(repo, "Create the file notes.txt containing exactly: one. Reply done.")
    check(not first.is_error, first.text[:300])
    sid = first.session_id
    second = acc.claude(repo, "Append a second line, two, to notes.txt, then run `git add notes.txt` and "
                              "`git commit -m \"add notes\"` as separate commands. Reply done.",
                        session=sid, resume=True)
    check(second.session_id == sid, f"resume gave session {second.session_id}, not {sid}")
    ended = acc.wait_ended(repo, sid)
    check(ended["turns"] == 2, f"expected 2 turns in one record, saw {ended['turns']}")
    check(sid in acc.trailers(repo), f"trailers {acc.trailers(repo)} lack {sid}")
    ids = [s["id"] for s in acc.ctx_json(repo, "sessions", "--full")["results"]]
    check(ids == [sid], f"expected one session record, saw {ids}")
    return f"session {sid[:8]}: one record, 2 turns, commit linked"


def capture_compaction(acc: Acc) -> str:
    repo = acc.repo("compact")
    init_repo(acc, repo)
    first = acc.claude(repo, "Create the file a.txt containing exactly: a. Reply done.")
    sid = first.session_id
    second = acc.claude(repo, "/compact", session=sid, resume=True)
    acc.wait_ended(repo, sid)
    hooks = acc.ctx_json(repo, "stats", "hooks")["results"][0]["events"]
    if not any(e["event"] == "PreCompact" for e in hooks):
        raise Manual(f"headless `/compact` did not run the PreCompact hook (claude said: "
                     f"{second.text.strip()[:120]!r}); compact a long session interactively")
    snaps = list((acc.store(repo) / "raw" / "sessions").glob(f"*-pc*-{sid}.jsonl"))
    check(snaps, "PreCompact ran but no pre-compact snapshot of the transcript was stored")
    return f"PreCompact ran; {len(snaps)} snapshot(s) beside the final copy"


def capture_subagent(acc: Acc) -> str:
    repo = acc.repo("subagent")
    init_repo(acc, repo)
    done = acc.claude(repo, "Use the Agent tool to start one subagent that reads README.md and reports its "
                            "first line. Do the reading only through the subagent. Reply with that line.",
                      tools="Read Agent Task")
    sid = done.session_id
    acc.wait_ended(repo, sid)
    folder = acc.store(repo) / "raw" / "subagents" / sid
    copies = wait_for(lambda: list(folder.glob("*.jsonl")) if folder.is_dir() else [],
                      f"a subagent transcript copy (tools used: {done.tools})")
    shown = acc.ctx(repo, "show", sid).stdout
    check("subagent" in shown, "ctx show does not mention the subagent")
    agent = copies[0].stem
    alone = acc.ctx(repo, "show", sid, "--subagent", agent).stdout
    check(alone.strip(), "ctx show --subagent printed nothing")
    return f"{len(copies)} subagent transcript(s); show and show --subagent render"


def capture_concurrent(acc: Acc) -> str:
    repo = acc.repo("concurrent")
    init_repo(acc, repo)
    runs = [acc.claude_start(repo, f"Create the file {n}.txt containing exactly: {n}. Reply done.")
            for n in ("alpha", "beta")]
    done = [acc.claude_finish(sid, proc) for sid, proc in runs]
    for d in done:
        check(not d.is_error, d.text[:300])
    seen = {}
    for (sid, _), name in zip(runs, ("alpha.txt", "beta.txt")):
        seen[name] = acc.wait_ended(repo, sid)
        check(seen[name]["files_touched"] == [name],
              f"session {sid[:8]} touched {seen[name]['files_touched']}, wanted [{name!r}]")
        raw = list((acc.store(repo) / "raw" / "sessions").glob(f"*-{sid}.jsonl"))
        check(len(raw) == 1, f"session {sid[:8]}: {len(raw)} transcript copies")
    return "two sessions, two records, each with only its own file and transcript"


def _linked(acc: Acc, repo: Path, target: str, sid: str | None) -> dict:
    block = acc.ctx_json(repo, "why", target)["results"][0]
    if sid is None:
        check(not block["linked"], f"{target} unexpectedly linked: {block['sessions']}")
    else:
        check(block["linked"] and any(s["id"] == sid and s["available"] for s in block["sessions"]),
              f"why {target} does not show session {sid}: {block['sessions']}")
    return block


def trace_amend(acc: Acc) -> str:
    repo = acc.repo("amend")
    init_repo(acc, repo)
    sid = acc.commit_session(repo, "f.txt", "hello", "add f").session_id
    old = acc.git(repo, "rev-parse", "HEAD")
    acc.checkpoint(repo, old)
    acc.git(repo, "commit", "--amend", "--no-edit", "-q", env={"GIT_COMMITTER_DATE": "2030-01-01T00:00:00Z"})
    new = acc.git(repo, "rev-parse", "HEAD")
    check(new != old, "amend produced the same commit")
    check(sid in acc.trailers(repo), "amended commit lost its AI-Session trailer")
    acc.checkpoint(repo, new)
    _linked(acc, repo, "f.txt:1", sid)
    return f"amended {old[:8]} -> {new[:8]}: trailer, checkpoint and why keep session {sid[:8]}"


def trace_rebase(acc: Acc) -> str:
    repo = acc.repo("rebase")
    init_repo(acc, repo)
    acc.git(repo, "switch", "-q", "-c", "feature")
    sid = acc.commit_session(repo, "g.txt", "feature", "add g").session_id
    old = acc.git(repo, "rev-parse", "HEAD")
    acc.checkpoint(repo, old)
    acc.git(repo, "switch", "-q", "main")
    (repo / "m.txt").write_text("main\n")
    acc.git(repo, "add", "m.txt")
    acc.git(repo, "commit", "-q", "-m", "main moves")
    acc.git(repo, "switch", "-q", "feature")
    acc.git(repo, "rebase", "main")
    new = acc.git(repo, "rev-parse", "HEAD")
    check(new != old, "rebase kept the same commit")
    check(sid in acc.trailers(repo), "rebased commit lost its AI-Session trailer")
    acc.checkpoint(repo, new)
    _linked(acc, repo, "g.txt:1", sid)
    return f"rebased {old[:8]} -> {new[:8]}: trailer, checkpoint and why keep session {sid[:8]}"


def trace_no_session(acc: Acc) -> str:
    repo = acc.repo("nosession")
    init_repo(acc, repo)
    (repo / "plain.txt").write_text("by hand\n")
    acc.git(repo, "add", "plain.txt")
    acc.git(repo, "commit", "-q", "-m", "by hand")
    check(not acc.trailers(repo), f"commit without a session got trailers {acc.trailers(repo)}")
    sha = acc.git(repo, "rev-parse", "HEAD")
    time.sleep(2)
    check(not (acc.store(repo) / "records" / "checkpoints" / f"{sha}.json").exists(), "checkpoint for an unlinked commit")
    out = acc.ctx(repo, "why", "plain.txt:1").stdout
    check("no linked session" in out, f"why does not mark the commit unlinked: {out[:300]}")
    return "commit succeeded, no trailer, no checkpoint, why says no linked session"


def trace_aborted(acc: Acc) -> str:
    repo = acc.repo("aborted")
    init_repo(acc, repo)
    sid = "abort-" + uuid.uuid4().hex[:8]
    acc.hook(repo, "SessionStart", sid)
    (repo / "x.txt").write_text("x\n")
    acc.git(repo, "add", "x.txt")
    before = acc.git(repo, "rev-parse", "HEAD")
    done = acc.run(["git", "commit"], repo, env={"GIT_EDITOR": "true"})
    check(done.returncode != 0, "commit with an unedited message did not abort")
    check(acc.git(repo, "rev-parse", "HEAD") == before, "an aborted commit moved HEAD")
    editor = acc.tmp / "editor.sh"
    editor.write_text("#!/bin/sh\nprintf 'typed message\\n' > \"$1.new\"; cat \"$1\" >> \"$1.new\"; mv \"$1.new\" \"$1\"\n")
    editor.chmod(0o755)
    acc.git(repo, "commit", "-q", env={"GIT_EDITOR": str(editor)})
    check(acc.trailers(repo) == [sid], f"editor-flow commit trailers: {acc.trailers(repo)}")
    acc.hook(repo, "SessionEnd", sid)
    return "empty message aborted with no commit; the typed message was linked by commit-msg"


def trace_merge(acc: Acc) -> str:
    repo = acc.repo("merge")
    init_repo(acc, repo)
    acc.git(repo, "switch", "-q", "-c", "feature")
    (repo / "feat.txt").write_text("feature\n")
    acc.git(repo, "add", "feat.txt")
    acc.git(repo, "commit", "-q", "-m", "feature work")
    acc.git(repo, "switch", "-q", "main")
    (repo / "main.txt").write_text("main\n")
    acc.git(repo, "add", "main.txt")
    acc.git(repo, "commit", "-q", "-m", "main work")
    sid = "merge-" + uuid.uuid4().hex[:8]
    acc.hook(repo, "SessionStart", sid)
    acc.git(repo, "merge", "--no-ff", "-m", "merge feature", "feature")
    parents = acc.git(repo, "rev-list", "--parents", "-1", "HEAD").split()[1:]
    check(len(parents) == 2, f"HEAD is not a merge commit: {parents}")
    check(acc.trailers(repo) == [sid], f"merge commit trailers: {acc.trailers(repo)}")
    # Git runs post-merge, not post-commit, for a merge: the trailer is the link, no checkpoint is written.
    acc.hook(repo, "SessionEnd", sid)
    out = acc.ctx(repo, "why", "feat.txt:1").stdout
    check("no linked session" in out, "blame of merged-in lines moved to the merge commit")
    return "merge succeeded, linked with the active session; blame still points at the original commit"


def why_show_forget(acc: Acc) -> str:
    repo = acc.repo("why")
    init_repo(acc, repo)
    marker = "MARKER" + uuid.uuid4().hex[:8]
    done = acc.commit_session(repo, "calc.py", "def add(a, b): return a + b", "add calc", marker=marker)
    sid = done.session_id
    acc.wait_ended(repo, sid)
    acc.checkpoint(repo, acc.git(repo, "rev-parse", "HEAD"))
    text = acc.ctx(repo, "why", "calc.py:1").stdout
    check(sid in text and marker in text and "RETRIEVED DATA" in text,
          f"why output lacks session, prompt or frame: {text[:400]}")
    check(sid in acc.ctx(repo, "why", "calc.py#add").stdout, "why FILE#symbol does not find the session")
    check(len(acc.ctx(repo, "--limit", "1", "why", "calc.py").stdout) <= len(text) + 400, "--limit grew the answer")
    (repo / "calc.py").write_text((repo / "calc.py").read_text() + "\n# local edit\n")
    kinds = [b["type"] for b in acc.ctx_json(repo, "why", "calc.py")["results"]]
    check("uncommitted" in kinds, f"uncommitted changes not reported: {kinds}")
    acc.git(repo, "checkout", "--", "calc.py")
    check(marker in acc.ctx(repo, "show", sid).stdout, "ctx show lacks the prompt")
    check(acc.ctx(repo, "show", sid, "--turns").stdout.strip(), "ctx show --turns printed nothing")
    listed = acc.ctx_json(repo, "sessions", "--file", "calc.py")["results"]
    check([s["id"] for s in listed] == [sid], f"sessions --file: {[s['id'] for s in listed]}")
    dry = acc.ctx(repo, "forget", sid, "--dry-run").stdout
    check("Would remove" in dry and acc.session(repo, sid), "forget --dry-run removed something")
    acc.ctx(repo, "forget", sid)
    check(acc.session(repo, sid) is None, "session still listed after forget")
    check(not list((acc.store(repo) / "raw").rglob(f"*{sid}*")), "raw files left after forget")
    after = acc.ctx(repo, "why", "calc.py:1").stdout
    check("not available" in after, f"why after forget: {after[:300]}")
    check(sid in acc.trailers(repo), "forget rewrote Git history")
    return "why (line, symbol, uncommitted), show, show --turns, sessions --file, forget dry-run and real"


def sharing(acc: Acc) -> str:
    bare = acc.tmp / "remote.git"
    bare.mkdir()
    acc.git(bare, "init", "-q", "--bare", "-b", "main")
    repo = acc.repo("share1")
    init_repo(acc, repo)
    acc.git(repo, "remote", "add", "origin", str(bare))
    acc.git(repo, "add", "-A")
    acc.git(repo, "commit", "-q", "-m", "wscontext config")
    acc.git(repo, "push", "-q", "origin", "main")
    time.sleep(3)
    check("wscontext/history" not in acc.git(repo, "ls-remote", "origin"), "history reached the remote before sharing was on")
    acc.ctx(repo, "sync", "--setup", "--remote", "origin")
    acc.git(repo, "add", ".wscontext/config.toml")
    acc.git(repo, "commit", "-q", "-m", "share history")
    sid = acc.commit_session(repo, "shared.py", "print(1)", "add shared").session_id
    acc.wait_ended(repo, sid)
    acc.checkpoint(repo, acc.git(repo, "rev-parse", "HEAD"))
    acc.git(repo, "push", "-q", "origin", "main")
    wait_for(lambda: "refs/heads/wscontext/history" in acc.git(repo, "ls-remote", "origin"),
             "the pre-push hook to push the history branch")
    acc.ctx(repo, "sync")
    status = acc.ctx(repo, "sync", "--status").stdout
    check("0 ahead" in status, f"sync --status after the push: {status.splitlines()[0]}")
    refused = acc.ctx(repo, "forget", sid, code=1)
    check("already on the shared history" in refused.stderr + refused.stdout, "forget of a shared session was not refused")
    second = acc.tmp / "share2"
    acc.git(acc.tmp, "clone", "-q", str(bare), str(second))
    acc.identify(second)
    acc.ctx(second, "init")
    block = wait_for(lambda: _available(acc, second, sid), "the second clone to find the session", 30)
    return f"history pushed only after setup; clone 2 found session {sid[:8]}; forget refused ({block})"


def _available(acc: Acc, repo: Path, sid: str) -> str | None:
    block = acc.ctx_json(repo, "why", "shared.py:1")["results"][0]
    return "ok" if any(s["id"] == sid and s["available"] for s in block["sessions"]) else None


def stats(acc: Acc) -> str:
    repo = acc.repo("stats")
    init_repo(acc, repo)
    done = acc.claude(repo, "Create a.txt containing exactly: a and b.txt containing exactly: b, then read "
                            "a.txt, then run `git add a.txt b.txt` and `git commit -m \"add a and b\"` as "
                            "separate commands. Reply done.")
    check(not done.is_error, done.text[:300])
    sid = done.session_id
    acc.wait_ended(repo, sid)
    calls = len(done.tools)
    detail = wait_for(lambda: (d := acc.ctx_json(repo, "stats", "session", sid)["results"])
                      and sum(d[0]["tool_calls"].values()) == calls and d,
                      f"{calls} tool calls in stats (the session ran {done.tools})")
    check(len(detail[1]["turns"]) == 1, f"session has {len(detail[1]['turns'])} turns, wanted 1")
    data = acc.ctx_json(repo, "stats", "data")["results"][0]
    check(data["sessions"] == 1 and data["turns"] == 1, f"data: sessions {data['sessions']}, turns {data['turns']}")
    check(data["commits"] == 2 and data["linked"] == 1, f"data: {data['linked']} of {data['commits']} commits linked")
    turns = acc.ctx_json(repo, "stats", "turns")["results"][0]
    check(sum(turns["tool_calls"].values()) == calls, f"turns view: {turns['tool_calls']} vs {calls} calls")
    health = acc.ctx_json(repo, "stats", "hooks")["results"][0]
    events = {e["event"] for e in health["events"]}
    missing = {"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"} - events
    check(not missing and health["failures"] == 0, f"hooks: missing {missing}, failures {health['failures']}")
    for _ in range(2):
        acc.ctx(repo, "why", "a.txt:1")
    seen = acc.ctx_json(repo, "stats", "queries")["results"][0]["by_command"].get("why")
    check(seen == 2, f"queries view counted {seen} why calls, ran 2")
    return f"{calls} tool calls, 1 turn, 1 of 2 commits linked, hooks healthy, 2 why calls counted"


@dataclass
class Scenario:
    name: str
    run: Callable[[Acc], str]
    claude: bool
    what: str


SCENARIOS = [
    Scenario("setup", setup, False, "init chains an existing hook, doctor all ok, uninstall keeps data (§4)"),
    Scenario("capture-resume", capture_resume, True, "a resumed session is one record with both turns (§5)"),
    Scenario("capture-compaction", capture_compaction, True, "PreCompact snapshot; falls to manual if headless cannot compact (§5)"),
    Scenario("capture-subagent", capture_subagent, True, "a subagent transcript is captured and shown (§5, §8)"),
    Scenario("capture-concurrent", capture_concurrent, True, "two concurrent sessions stay separate (§5)"),
    Scenario("trace-amend", trace_amend, True, "an amended commit keeps its link (§6)"),
    Scenario("trace-rebase", trace_rebase, True, "a rebased commit keeps its link (§6)"),
    Scenario("trace-no-session", trace_no_session, False, "a commit with no session succeeds, unlinked (§6, §7)"),
    Scenario("trace-aborted", trace_aborted, False, "an aborted message makes no commit; the editor flow is linked (§6)"),
    Scenario("trace-merge", trace_merge, False, "a merge commit during a session is linked, blame unchanged (§6)"),
    Scenario("why-show-forget", why_show_forget, True, "why, show, sessions and forget (§7, §8)"),
    Scenario("sharing", sharing, True, "two clones of one bare remote (§9)"),
    Scenario("stats", stats, True, "ctx stats agrees with what the scenarios did (§10)"),
]

MANUAL = [
    "Capture from the VS Code extension and from an interactive terminal session (§5).",
    "A commit made from an IDE source-control panel during a session carries the trailer (§6).",
    "Compaction in a long interactive session stores a pre-compact snapshot (§5), unless capture-compaction passed.",
    "Enable the plugin for the team by hand in committed project settings; a teammate without it commits normally (§4).",
    "The agent actually runs `ctx why` before changing unfamiliar code, prompted by the CLAUDE.md block and the ctx skill (§7).",
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
            print(f"{s.name:20} {'claude -p' if s.claude else 'offline  '}  {s.what}")
        print("\nmanual checklist:", *MANUAL, sep="\n  - ")
        return 0

    plugin = locate_plugin(args.plugin_root, args.plugin_data)
    print(f"plugin root: {plugin.root}\nplugin version: {plugin.version}\nplugin data: {plugin.data}")
    print("candidates: " + (", ".join(f"{k}={v}" for k, v in plugin.candidates.items()) or "none found"))
    for tool in ("git", "uv") + (() if args.no_claude else ("claude",)):
        if shutil.which(tool) is None:
            sys.exit(f"{tool} is not on PATH")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true"}
    tmp = Path(tempfile.mkdtemp(prefix="wscontext-acceptance-")).resolve()
    print(f"working directory: {tmp}\n")
    acc = Acc(tmp, plugin, args.model, args.max_budget_usd, env)
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
