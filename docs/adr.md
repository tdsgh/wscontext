<!-- kit:core/docs/adr v1 -->
# Architecture decisions

What was decided, and what follows from it. What the system *is* today lives in
[architecture.md](architecture.md).

Entries are **append-only**. A reversed decision gets a new entry and the old
one is marked `Superseded by ADR-NNN` — never rewritten. Each entry states the
decision and its implications. `Guardrail` appears only where the bare
statement would read as an oversight someone might helpfully "fix". Extended
reasoning belongs in its own file, linked from the entry.

---

### ADR-001 — Capture runs on Claude Code hooks, never on agent instructions

**Decision.** Sessions are captured by Claude Code hooks that copy what Claude
Code already records. No skill or CLAUDE.md text asks the agent to record
anything.

**Implications.**
- Capture works whether or not the agent cooperates, and survives context
  compaction.
- Instructions to the agent exist only on the retrieval side (→ ADR-012).

**Reasoning.** [design-rationale.md § Why capture runs on hooks](design-rationale.md#why-capture-runs-on-hooks)

### ADR-002 — Transcripts are read only through a versioned adapter; Claude Code is the only agent

**Decision.** The Claude Code transcript is treated as an unstable, internal
format and parsed by one adapter that checks the format version. Claude Code is
the only agent with an adapter.

**Implications.**
- An unrecognized version degrades to raw-copy-only: the transcript is still
  stored, but fields derived from it (turn tokens, exchanges) are absent.
- Fields the adapter does not know are kept in raw data and ignored by indexes.
- Another agent means another adapter behind the same seam; nothing else
  changes.
- Each store covers one repository; cross-repository search is out of scope.

**Reasoning.** [design-rationale.md § Why build it rather than reuse](design-rationale.md#why-build-it-rather-than-reuse)

### ADR-003 — A commit links to its sessions through an `AI-Session` trailer plus a checkpoint record

**Decision.** `prepare-commit-msg` adds an `AI-Session: <id>` trailer per active
session; `post-commit` writes a checkpoint record keyed by the commit SHA.
`post-rewrite` writes checkpoints for the rewritten SHAs.

**Implications.**
- `git log` alone shows which session produced a commit, on any Git host.
- A commit made with no active session gets no trailer and no checkpoint.
- Amend, rebase and cherry-pick keep the trailer; the old checkpoint stays and
  a new one is added for the new SHA.

Superseded by ADR-025 for the hooks that add the trailer; the rest stands.

### ADR-004 — The runtime store lives under the Git common dir; only config and decisions live in the working tree

**Decision.** Raw data, records, indexes, state and logs live in
`<git-common-dir>/wscontext/`. Committed configuration and decision records
live in `.wscontext/` in the working tree.

**Implications.**
- All worktrees of a repository share one store.
- Nothing in the runtime store appears in `git status`, needs a gitignore entry,
  or is touched by checkout, stash or `git clean`.
- Deleting the clone deletes the local store; only sync (→ ADR-007) preserves it
  elsewhere.

**Reasoning.** [design-rationale.md § Why the store sits under the git common dir](design-rationale.md#why-the-store-sits-under-the-git-common-dir)

### ADR-005 — Raw transcripts and records are the source of truth; indexes are derived

**Decision.** Redacted raw transcripts and normalized records are the only
authoritative data. Every index and the code graph are rebuilt from them and
from the repository.

**Implications.**
- `ctx rebuild` regenerates every index; deleting an index is always safe.
- Indexes are never synced; each host builds its own.
- Index schemas can change without migrations: bump the version and rebuild.
- Record schemas carry a version field and are read by every later version.

### ADR-006 — Redaction happens on write; nothing unredacted is persisted

**Decision.** Transcripts, prompts, `ctx` query text and log lines pass through
the redactor before anything is written to the store.

**Implications.**
- Redaction rules live in `.wscontext/config.toml` and add to built-in rules.
- A redaction miss is fixed by `ctx forget` plus a rule change, not by editing
  stored files.
- Claude Code's own transcripts under the user's home are outside the harness's
  control.

**Guardrail.** There is no "raw mode" setting that skips redaction.

### ADR-007 — History is shared through an orphan branch written with Git plumbing

**Decision.** Raw transcripts and records are committed to the orphan branch
`wscontext/history` with `hash-object`, `mktree` and `commit-tree`, never
through the working tree.

**Implications.**
- Files on the branch are append-only and named by session ID or commit SHA, so
  merging two hosts' histories is a union and cannot conflict.
- Local ref updates are compare-and-swap, so concurrent sessions cannot lose
  each other's commits.
- Removing a session after it was pushed means rewriting the branch and
  force-pushing it.

**Reasoning.** [design-rationale.md § Why history lives on an orphan branch](design-rationale.md#why-history-lives-on-an-orphan-branch)

### ADR-008 — Sync is opt-in and pushes only alongside `git push` or on `ctx sync`

**Decision.** History stays local until a sync remote is set in
`.wscontext/config.toml`. Once set, the `pre-push` hook pushes the history ref
with the user's own pushes, and `ctx sync` pushes on demand. Session start and
`ctx` calls fetch, throttled.

**Implications.**
- A rejected history push fetches, union-merges and retries once, and never
  fails the user's code push.
- The sync remote can differ from `origin`, for public repositories.
- The setting is committed, so teammates inherit it.

**Guardrail.** Claude Code hooks never push.

**Reasoning.** [design-rationale.md § Why sync is opt-in](design-rationale.md#why-sync-is-opt-in)

### ADR-009 — Hooks never block the agent or a commit

**Decision.** Every hook exits 0 within a bounded time, logs its own failures,
and defers heavy work.

**Implications.**
- Every failure degrades to missing context, never to an error the agent or
  the user sees.
- Hook failures and latency are recorded and reported by `ctx stats hooks`
  (→ ADR-019).
- Tool-call capture runs as an async hook so it adds no latency to tool calls.

### ADR-010 — No daemon

**Decision.** Nothing stays running. Hooks append data and may spawn a short
detached worker; every `ctx` query first indexes any gap between the newest
records and the index watermark.

**Implications.**
- A missed hook or a record synced from another host is picked up at the next
  query.
- Work keys are content hashes, so re-indexing is idempotent.

### ADR-011 — One CLI, `ctx`, with budgeted, structured, data-framed output

**Decision.** A single `ctx` entrypoint serves agent commands (`why`, `find`,
`code`, `impact`, `show`, `sessions`) and admin commands (`init`, `stats`,
`sync`, `doctor`, `rebuild`, `forget`, `uninstall`, `hook`). Agent-facing
output has a token budget, carries type, ID, date and links per result, flags
superseded or stale results, and is framed as retrieved data.

**Implications.**
- `--json` is available on every read command.
- Text found in old transcripts is never presented as an instruction.
- There is no separate `harness` executable.

**Reasoning.** [design-rationale.md § Why results carry freshness and supersession](design-rationale.md#why-results-carry-freshness-and-supersession)

### ADR-012 — Retrieval is push plus pull, and adoption is measured

**Decision.** A `SessionStart` hook injects a short brief of relevant prior
work; a skill and a short CLAUDE.md block tell the agent when to call `ctx`.
Adoption is measured from captured data.

**Implications.**
- The CLAUDE.md block states triggers only; the command reference lives in the
  skill.
- Low adoption is answered by tighter triggers or a richer brief, not longer
  instructions.

**Reasoning.** [design-rationale.md § Why push and pull and why adoption is measured](design-rationale.md#why-push-and-pull-and-why-adoption-is-measured)

### ADR-013 — Indexes are SQLite in WAL mode; full-text first, vectors optional

**Decision.** The history index uses SQLite FTS5. Semantic search adds local
embeddings stored with sqlite-vec, merged by reciprocal rank fusion, and is
optional.

**Implications.**
- Concurrent sessions can write while the agent queries.
- Embeddings run locally; no text leaves the machine for indexing.
- Search works with FTS5 alone when no embedding model is installed.

### ADR-014 — The code graph is tree-sitter based, name-matched, and incremental

**Decision.** The graph stores files and symbols as nodes and `defines`,
`imports`, `calls`, `co_changes` and `touched_by` as edges, parsed with
tree-sitter and re-parsed per file when its content hash changes.

**Implications.**
- `calls` edges are matched by name and are ambiguous with overloads and
  duplicate names.
- Precise references (SCIP or LSP indexers) are added only for languages where
  the ambiguity hurts.
- Query time reflects uncommitted edits by comparing against the working tree.

**Reasoning.** [design-rationale.md § Why the code graph comes third](design-rationale.md#why-the-code-graph-comes-third)

### ADR-015 — Distillation is asynchronous and marked machine-written until reviewed

**Decision.** Decision records are extracted from finished sessions by an LLM
off the critical path, and stay marked machine-written until a person reviews
them.

**Implications.**
- Decision records carry status (active or superseded) and an optional link to
  the record they replace.
- Reviewed decision records can be committed to `.wscontext/decisions/` through
  normal pull requests.

**Reasoning.** [design-rationale.md § Why distill decisions instead of searching raw history](design-rationale.md#why-distill-decisions-instead-of-searching-raw-history)

### ADR-016 — Python 3.13, with a stdlib-only hook path

**Decision.** wscontext is written in Python 3.13. Code reachable from a hook
entrypoint imports only the standard library.

**Implications.**
- Configuration models on the hook path are dataclasses read with `tomllib`.
- Third-party packages (tree-sitter, sqlite-vec, embedding runtimes) are
  imported only by commands and background workers.

**Reasoning.** [design-rationale.md § Why Python with a stdlib-only hook path](design-rationale.md#why-python-with-a-stdlib-only-hook-path)

### ADR-017 — Distributed as a Claude Code plugin; repository wiring is an explicit `ctx init`

**Decision.** This repository is a plugin marketplace holding one plugin,
`wscontext`, which ships the Claude Code hooks, the `ctx` launcher in `bin/`,
and the skills. `ctx init` installs the Git hooks and the CLAUDE.md block in a
repository.

**Implications.**
- The launcher keeps a uv-managed environment in the plugin data directory and
  re-syncs it when the lockfile changes; `uv` is a prerequisite.
- Hooks call the launcher by absolute path under the plugin root.
- The launcher passes plugin paths to Python as arguments; application code
  reads no environment variables.
- Teammates get the plugin through committed `extraKnownMarketplaces` and
  `enabledPlugins` settings; where it is not enabled, no Claude Code hook fires.
- Installed Git hooks chain to existing ones and exit 0 when the launcher is
  missing.

**Guardrail.** The plugin's environment is a runtime artifact outside the repo,
not a second development venv.

**Reasoning.** [design-rationale.md § Why a plugin](design-rationale.md#why-a-plugin)

Superseded by ADR-023 for the launcher re-sync and Git-hook launcher implications; the rest stands.

### ADR-018 — Monitoring is a CLI report

**Decision.** Statistics are shown by `ctx stats` and its views (`data`,
`code`, `turns`, `queries`, `hooks`, `session <id>`), as terminal tables or
`--json`. There is no web UI.

**Implications.**
- Any dashboard is built outside wscontext on the `--json` output.

### ADR-019 — Monitoring reads hooks, `ctx`'s own call log, and the transcript adapter; not OpenTelemetry

**Decision.** Tool calls and turn timing come from hooks, `ctx` call details
from `ctx`'s own log, and token figures from the transcript adapter on a
best-effort basis.

**Implications.**
- Raw hook and `ctx` call logs are per host and never synced.
- Per-session counts (tool calls by type, `ctx` calls, turn tokens) are written
  into the session record, so team-wide adoption figures survive sync.
- Token figures are absent for sessions whose transcript version the adapter
  does not recognize.
- Baseline tool-call figures are collected from stage 1 on.

**Reasoning.** [design-rationale.md § Why not OpenTelemetry for monitoring](design-rationale.md#why-not-opentelemetry-for-monitoring)

Superseded by ADR-029 for the baseline implication; the rest stands.

### ADR-020 — Portable across macOS, Linux and Windows

**Decision.** All logic is in Python. Shell is limited to one-line launcher and
Git-hook shims, shipped in both `sh` and `.cmd` form.

**Implications.**
- Locks, detached workers and paths use portable mechanisms.
- The offline test suite runs on Windows CI from stage 1; a real Windows
  click-through happens in the Finalize stage.

**Guardrail.** No POSIX-only calls (`fork`, `fcntl`, `setsid`) in application code.

**Reasoning.** [design-rationale.md § Why portable from stage 1](design-rationale.md#why-portable-from-stage-1)

Superseded by ADR-021 for the Windows CI implication; the rest stands.

Superseded by ADR-022 for the shell-limits decision; the rest stands.

### ADR-021 — Tests run on macOS only until Finalize; portability is held by a guard test

**Decision.** Stages 1 to 4 run the offline suite on macOS only, with no CI. A
portability guard test and platform branches written as pure functions keep
Windows support honest; the first Windows run is on a Windows VM in the
Finalize stage.

**Implications.**
- A test scans application code for POSIX-only modules and calls and fails the
  suite when one appears.
- Each platform-specific behaviour (detach flags, launcher path, hook shim
  text) is a pure function of the platform, tested for both `win32` and POSIX
  on macOS.
- Windows-only defects can surface late, in Finalize.
- CI is added only when a runner exists; the Gitea Actions runner can run on
  the macOS host.

**Reasoning.** [design-rationale.md § Why portable from stage 1](design-rationale.md#why-portable-from-stage-1)

### ADR-022 — Shell is limited to the plugin launchers and the Git-hook shims

**Decision.** All logic is in Python. Shell is limited to the plugin launcher
(`bin/ctx` in POSIX `sh`, `bin/ctx.cmd`), the stable launcher `ctx init` writes,
and the Git-hook shims, which are `sh` on every platform.

**Implications.**
- The launcher is a short script, not a one-liner: it keeps the uv environment,
  takes a `mkdir` build lock, and execs `ctx` (bin/ctx:5-19).
- For `hook` and `git-hook` calls it never waits for a build and exits 0 when
  `uv` or the environment is missing (bin/ctx:13-16).
- Git-hook shims have no `.cmd` form: Git runs hook files through `sh`, Git for
  Windows included (src/wscontext/githooks/install.py:1-4).
- `bin/ctx.cmd` is not exercised before Finalize (bin/ctx.cmd:2; → ADR-021).

**Guardrail.** No logic moves into a launcher or shim.

### ADR-023 — Git hooks call a stable launcher in the data directory; the plugin environment is rebuilt from a stamp

**Decision.** `ctx init` writes `<data>/bin/ctx`, and every Git-hook shim embeds
that path. It forwards to the plugin launcher under the root recorded in
`<data>/plugin-root`, which every plugin launcher run refreshes. The launcher
rebuilds `<data>/venv` when the stamp in `<data>/venv/.lock-hash` (plugin root,
`cksum` of `uv.lock`, `cksum` of `.claude-plugin/plugin.json`) differs.

**Implications.**
- Git hooks survive plugin updates without re-running `ctx init`
  (src/wscontext/cli/init.py:1-6, 34-49).
- The rebuild is `uv sync --frozen --no-dev --no-editable --reinstall-package wscontext`
  (bin/ctx:7). The installed package is a copy of the plugin sources, refreshed
  when `plugin.json` or the plugin root changes.
- The data directory comes from `--plugin-data`. Without it (a Bash-tool call)
  the launcher uses `~/.claude/plugins/data/wscontext-wscontext`, which holds
  only while marketplace and plugin are both named `wscontext` (bin/ctx:5).
- `ctx doctor` flags a shim that points into `plugins/cache/`, is not
  executable, is not the stable launcher, or records a root that is gone
  (src/wscontext/cli/doctor.py:71-86).

**Guardrail.** A shim never points into the versioned plugin cache.

### ADR-024 — wscontext never edits Claude Code settings; team rollout is manual

**Decision.** Neither the plugin install nor `ctx init` writes to
`.claude/settings.json`. The `init` skill tells the operator to add
`enabledPlugins` and `extraKnownMarketplaces` to the committed file by hand
(skills/init/SKILL.md:15-20).

**Implications.**
- A teammate gets the plugin once that entry is committed. Each teammate still
  runs `ctx init` in their clone.
- No code path under `src/` references a settings file.

### ADR-025 — The `AI-Session` trailer is added by `prepare-commit-msg` and `commit-msg`

**Decision.** Both hooks run the same handler. It adds one trailer per active
session in the worktree, and does nothing during a rebase or `git am`.
`prepare-commit-msg` also skips an empty message. The editor flow is linked by
`commit-msg`, once the message has text.

**Implications.**
- Replayed commits keep their original trailers and get no new ones
  (src/wscontext/githooks/handlers.py:92-106; HANDLERS at :143-144).
- The checkpoint stays in `post-commit` and `post-rewrite` (→ ADR-003).

**Guardrail.** `commit-msg` is not redundant: Git would commit a trailer-only
message after an aborted editor.

### ADR-026 — `ctx forget` squashes history-branch commits not yet pushed

**Decision.** Forgetting a session that is not on a remote removes its files
from the branch, then replaces every unpushed history commit with one commit
holding the current tree.

**Implications.**
- Blobs held only by earlier local commits become unreachable, so a later push
  cannot send them (src/wscontext/cli/forget.py:110-115;
  src/wscontext/history/branch.py:151-173).
- A session already on a remote is refused (src/wscontext/cli/forget.py:60-67; → ADR-007).
- Logs are not edited. Their lines are redacted at write (→ ADR-006).

### ADR-027 — `hook`, `worker` and `git-hook` are internal commands on a stdlib-only path

**Decision.** `ctx hook <event>`, `ctx worker …` and `ctx git-hook <hook>` sit
beside the user-facing commands. They load neither argparse nor the store
commands.

**Implications.**
- Shims and `hooks.json` call them. They are not part of the documented
  surface (src/wscontext/cli/main.py:1-4, 27, 98-99; → ADR-011, ADR-016).

### ADR-028 — The test suite runs in parallel with pytest-xdist

**Decision.** `pytest-xdist` is a dev dependency, and `addopts` is
`-n auto --dist load` (pyproject.toml:14-16, 29).

**Implications.**
- The suite and the timing-sensitive repeat rule are in root `CLAUDE.md` § Test gates.

### ADR-029 — Stage gates check requirements; real use and value are measured at Alpha and Beta

**Decision.** A stage gate passes when the stage's acceptance scenarios pass:
the offline suite, a scripted run against the installed plugin with real Git
and headless Claude Code, and a short manual checklist. Real use starts at
Alpha, after stage 3; Beta, after stage 4, adds a second developer. Value is
measured by a fixed task set run headless with wscontext's brief and search
off, then on.

**Implications.**
- No stage waits on calendar time.
- The scripted acceptance run and the benchmark spend API usage and need a
  logged-in Claude Code.
- Stage 2 adds a configuration switch that turns the brief and search off.
- Stage-1 sessions are not a baseline; the benchmark's off arm is.
