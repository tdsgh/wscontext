<!-- kit:core/docs/architecture v1 -->
# Architecture

What wscontext is, today. Decisions behind it are in [adr.md](adr.md); the
reasoning behind those is in [design-rationale.md](design-rationale.md).

Stage 1 is built; § Stages tracks what each later stage delivers. Parts planned
for stages 2 to 5 are marked.

## Purpose and data flow

wscontext is a local harness for Claude Code. It captures every agent session,
links sessions to the Git commits they produced, indexes them alongside a code
graph, and gives the agent one tool, `ctx`, to search both. Nothing leaves the
machine except through an opt-in Git remote.

```
 Claude Code session ──hooks──▶ capture ──▶ raw/ + records/ ──▶ history.db ─┐
        ▲                          ▲          (source of truth)              ├─▶ ctx ──▶ agent context
        │                          │                             graph.db ──┘          (push brief at
        │                 git hooks (trailer,        ▲                                  SessionStart,
        │                 checkpoint, rewrite)       └── repository working tree       pull via skill)
        │                                                + git log
        └───────────────────────────────── ctx output ◀────────────────────────────────┘

 raw/ + records/ ◀──── wscontext/history orphan branch ────▶ sync remote   (opt-in)
```

History flows down the left path, code structure down the right; they meet only
in `ctx`.

## Requirements

Goals:

- Capture every Claude Code session deterministically (→ ADR-001).
- Link each session to the commits it produced (→ ADR-003).
- Answer in one call each: what was done before (`ctx find`), why code exists
  (`ctx why`), what a change affects (`ctx impact`).
- Share history across hosts and teammates through Git alone, with every
  derived artifact rebuildable (→ ADR-005, ADR-007).
- Report statistics on collected data, code, Claude Code turns and `ctx` calls
  (→ ADR-018).
- Never block the agent or a commit (→ ADR-009).
- Run on macOS, Linux and Windows (→ ADR-020).

Non-goals:

- Agents other than Claude Code (→ ADR-002). Other agents in the same IDE, such
  as Copilot Chat, are not captured.
- Cross-repository search.
- A web UI (→ ADR-018).
- Compiler-grade code intelligence (→ ADR-014).

## Topology

Three places hold wscontext's parts.

**Per machine — the plugin.** Installed from the marketplace in this repository
(→ ADR-017). Claude Code keeps the plugin under its plugin cache and gives it a
persistent data directory, where the launcher keeps a uv-managed environment.
Works the same from the terminal, the VS Code extension and any client that
fires Claude Code hooks. The data directory holds `venv/`, `bin/ctx` (the stable
launcher `ctx init` writes), `plugin-root` and the build `lock` (→ ADR-023).
Claude Code creates the directory on first use in a session, not at install.
The plugin's `bin/` is on `PATH` only inside Claude Code's Bash tool and `!`
commands, not in the user's terminal. `CLAUDE_PLUGIN_*` variables reach hook
commands only, not the Bash tool. Uninstalling the plugin does not stop hooks in
sessions already running.

**Per repository — wiring and store.** `ctx init` installs Git hooks (chained to
existing ones) and the CLAUDE.md block. The store is split (→ ADR-004):

```
.wscontext/                                        committed, working tree
  config.toml                                      settings, redaction rules, sync remote
  decisions/<id>.md                                reviewed decision records (stage 4)

<git-common-dir>/wscontext/                        local, shared by all worktrees
  raw/sessions/<date>-<session-id>.jsonl           redacted transcripts           synced
  raw/sessions/<date>-pc<stamp>-<session-id>.jsonl PreCompact snapshots           host-local
  raw/subagents/<session-id>/<agent-id>.jsonl      redacted subagent transcripts  synced
  records/sessions/<session-id>.json               normalized sessions            synced
  records/checkpoints/<sha>.json                   commit ↔ session links         synced
  index/history.db                                 FTS5 (+ vectors) (stage 2)     derived
  index/graph.db                                   code graph (stage 3)           derived
  state/    active.json, turns/<session>.json, materialized.json, last_fetch, locks/, rewrites/   host-local
  logs/     hooks.jsonl, ctx-calls.jsonl (one rotated generation each, .1)                        host-local
```

**Per sync remote — the history branch.** `wscontext/history` is an orphan
branch holding `raw/` and `records/` only, written with Git plumbing
(→ ADR-007). It is pushed only when sync is configured, alongside the user's
`git push` or on `ctx sync` (→ ADR-008). A fresh clone recovers history with
`ctx init`, which fetches the branch; indexes rebuild on the first query
(→ ADR-010).

### Hook events

| Hook | Fires when | Records |
|---|---|---|
| `SessionStart` | A session starts, resumes, clears or compacts | Session record (ID, branch, HEAD, start, author) kept on resume; marks the session active for the worktree; detached history fetch when sync is configured; injects the brief (stage 2+) |
| `UserPromptSubmit` | A prompt is sent | Turn start time |
| `PostToolUse` (all tools, async) | A tool call finishes | Tool name, file path for edits, `ctx` invocation and output size |
| `PreCompact` | Before context compaction | Transcript snapshot while `capture.precompact_snapshot` is on (default); open question in [wscontext-phase-1.md](hist/wscontext-phase-1.md) |
| `Stop` | A turn ends | Turn end time; detached worker copies and redacts the transcript |
| `SessionEnd` | A session ends | Deactivates the session; detached worker writes the final redacted copy, subagent copies, session record with counts and turns, and commits them to the history branch |
| Git `prepare-commit-msg`, `commit-msg` | A commit is being made | `AI-Session` trailer per active session in this worktree; skipped during rebase and `am`; `prepare-commit-msg` skips an empty message (→ ADR-025) |
| Git `post-commit` | After a commit | Detached worker writes the checkpoint and commits it to the history branch, when `HEAD` has trailers |
| Git `post-rewrite` | After amend or rebase | Queues old to new pairs; detached worker writes checkpoints for the rewritten SHAs |
| Git `pre-push` | Before a push | Detached worker pushes `wscontext/history` when sync is configured; the shim runs wscontext only if the chained original exited 0 |
| Git `post-merge` | Merge | A merge commit created here that carries `AI-Session` trailers is checkpointed like `post-commit`; a fast-forward and `--squash` write nothing. The graph refresh lands in stage 3 |
| Git `post-checkout` | Branch switch | Installed; the handler is a no-op until stage 3 |

## Data model

Every record carries a schema version and the Git author who produced it.

- **Session** — ID, agent name, branch, HEAD at start and end, start and end
  time, worktree, files touched, subagent IDs, and per-session counts: turns,
  tool calls by tool, `ctx` calls by command, turn tokens where available.
- **Event** — one prompt, assistant message or tool call in a session, with role
  and timestamp. Lives in the raw transcript; the index stores exchanges
  (prompt plus response).
- **Checkpoint** — commit SHA, the session IDs behind it, diff stats, and the
  SHA it was rewritten from, if any.
- **Decision** — what was chosen and why, rejected alternatives, linked files
  and symbols, status (active or superseded), the decision it replaces, and
  whether it is machine-written or reviewed (→ ADR-015).
- **Hook run** (host log) — event, duration, outcome.
- **`ctx` call** (host log) — timestamp, source (`cli` or `hook`), cwd, redacted
  argv, exit code, latency, output bytes, result count, flags. The hook-side
  line carries the session ID; stats join the two by argv and time (→ ADR-019).

## Components

| Component | What it is | Where | Stage |
|---|---|---|---|
| `ctx` CLI | Single entrypoint for agent and admin commands (→ ADR-011); internal `hook`, `worker`, `git-hook` (→ ADR-027) | `src/wscontext/cli/` | 1+ |
| Config | Loads `.wscontext/config.toml` into dataclasses (→ ADR-016) | `src/wscontext/config.py` | 1 |
| Store | Resolves the store under the Git common dir; reads and writes records | `src/wscontext/store/` | 1 |
| Hook handlers | One handler per Claude Code event; exit 0, bounded time (→ ADR-009) | `src/wscontext/capture/` | 1 |
| Transcript adapter | Versioned parser of Claude Code transcripts (→ ADR-002) | `src/wscontext/adapters/` | 1 |
| Redactor | Pattern rules applied before every write (→ ADR-006) | `src/wscontext/redact/` | 1 |
| History branch | Plumbing writer, union merge, fetch and push (→ ADR-007, ADR-008) | `src/wscontext/history/` | 1 |
| Git hooks | Shims plus handlers for trailer, checkpoint, rewrite, push; chaining installer | `src/wscontext/githooks/` | 1 |
| Stats | `ctx stats` views over records, logs and indexes (→ ADR-018, ADR-019) | `src/wscontext/stats/` | 1+ |
| History index | FTS5, then vectors; incremental with query-time gap check (→ ADR-013) | `src/wscontext/index/` (planned) | 2, 4 |
| Session brief | `SessionStart` context from prior work (→ ADR-012) | `src/wscontext/capture/` (planned) | 2 |
| Code graph | tree-sitter nodes and edges, incremental by content hash (→ ADR-014) | `src/wscontext/graph/` (planned) | 3 |
| Distiller | Async LLM extraction of decision records (→ ADR-015) | `src/wscontext/distill/` (planned) | 4 |
| Plugin package | Marketplace and plugin manifests, `hooks/hooks.json`, `bin/ctx`, `bin/ctx.cmd`, stable launcher, skills (→ ADR-017) | repo root: `.claude-plugin/`, `hooks/`, `bin/`, `skills/` | 1 |

## Stages

Stage 1 is built and passed its acceptance gate. Stages and their gates
are in [roadmap.md](roadmap.md).

| Stage | Delivers | Requirements | Status |
|---|---|---|---|
| 1 — Capture and trace | Plugin, capture, redaction, commit linking, history sync, `ctx why/sessions/show/forget`, `ctx stats` | [wscontext-phase-1.md](hist/wscontext-phase-1.md) | built |
| 2 — History search | FTS5 index, `ctx find`, session brief, skill and CLAUDE.md block, adoption stats | [wscontext-phase-2.md](wscontext-phase-2.md) | not built |
| 3 — Code graph | Graph, `ctx code`, `ctx impact`, code stats | not written | not built |
| 4 — Distillation and semantic search | Decision records with supersession, local embeddings, hybrid ranking | not written | not built |
| 5 — Finalize | Release, Windows verification, purge and uninstall polish | not written | not built |

## Known issues / accepted debt

- The transcript format is internal to Claude Code and changes between versions;
  derived fields disappear until the adapter catches up (→ ADR-002).
- `calls` edges are name-matched and ambiguous (→ ADR-014).
- Removing a session after it was pushed requires rewriting and force-pushing
  the history branch (→ ADR-007).
- Host squash-merges may drop `AI-Session` trailers; checkpoints then exist only
  for the pre-merge SHAs.
- Redaction hits found when materializing shared history are logged as a
  warning, not persisted, so `ctx stats data` does not count them
  (src/wscontext/history/sync.py:184-188).
- `ctx forget` does not edit host logs; their lines were redacted at write
  (src/wscontext/cli/forget.py).
- Page boundaries of `ctx show` are computed per call from the growing raw copy,
  so they shift while the session is active (src/wscontext/cli/show.py:16, 77).
- `post-checkout` is installed but does nothing until stage 3; `post-merge`
  checkpoints a merge commit made here and refreshes nothing until stage 3
  (src/wscontext/githooks/handlers.py:138).
- `bin/ctx.cmd` and the Windows path are untested until Finalize (bin/ctx.cmd:2;
  → ADR-021).
- `.claude-plugin/plugin.json` has no author field; plugin validation warns.
- Hooks keep firing in sessions that were already running when the plugin is
  uninstalled.
- Nothing bounds the store's size. Raw transcripts are kept in full;
  `retention.raw_days` is not enforced (pruning is stage 5); Claude Code's own
  bookkeeping entries (system-prompt snapshots, API request records, skill and
  tool listings) are a large share of a short session. With sync on,
  `wscontext/history` is a branch under `refs/heads/`, so a plain clone of the
  sync remote downloads all shared history (→ ADR-007).
- A session that never sends `SessionEnd` (a closed VS Code conversation, a
  terminal session that is gone) keeps the local transcript copy written at
  each `Stop` but gets no final copy, no counts and no history-branch commit,
  so it is never shared; it stays in new commits' trailers until
  `capture.stale_session_minutes` pass (src/wscontext/capture/handlers.py:91,
  src/wscontext/githooks/handlers.py:40). The fix is planned for stage 2.
