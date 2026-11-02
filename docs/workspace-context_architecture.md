# Local Entire: Architecture & Critique

Nov 2, 2026 · @Dmitry

## Purpose and scope

Local Entire is a self-contained harness for Claude Code that captures agent sessions, links them to Git commits, indexes them alongside a code graph, and gives the agent one search tool over both. Nothing leaves the machine, and it works with any Git host.

Goals:

- Capture every Claude Code session deterministically, with no reliance on the agent remembering to do it.
- Link each session to the commits it produced, so code can be traced back to its reasoning.
- Answer three questions in one tool call each: what was done before, why it was done, and what a change affects.
- Share history across hosts and teammates through Git itself, with no extra server, and keep every derived artifact (indexes, graph) rebuildable from it.

Non-goals for the first version:

- Agents other than Claude Code (design an adapter seam, but build one adapter).
- Cross-repository search or a web UI.
- Compiler-grade code intelligence. The graph aims for useful, not perfect.

## Challenging the idea

The idea is sound, but three parts of the original sketch would fail in practice: capture through instructions, storing raw history in the working tree, and assuming the agent will use the tool. Each challenge below changes the design.

**1. Capture must not depend on instructions.** Skills and AGENTS.md are suggestions the model may skip, forget after context compaction, or follow inconsistently. An agent summarizing its own session is also biased toward its own narrative. Use Claude Code hooks instead: they run deterministically on every lifecycle event, and Claude Code already writes a full JSONL transcript whose path each hook receives. Instructions belong only on the retrieval side.

**2. Raw history is mostly noise.** Transcripts are dominated by tool output, file dumps and dead ends. Searching them returns plausible but low-value matches and burns context. The valuable unit is a decision: what was chosen, why, and what was rejected. Plan for a distillation step, and index raw text only as a fallback.

**3. Old reasoning can be wrong now.** A decision reversed last month still matches today's query. Without supersession and freshness signals, retrieval actively misleads the agent. Every result needs a date, a status (active or superseded), and a check of whether the code it refers to still exists.

**4. The agent defaults to grep.** Agents are trained on grep, read and glob, and an extra tool competes with habit. Entire itself documents the case where the agent ignores its graph and greps anyway. Two mitigations: inject relevant context automatically at session start (push, not pull), and measure adoption from the transcripts you already capture.

**5. The graph may not pay off.** In a repo of a few thousand lines, ripgrep is fast and cheap, and a graph adds build cost and staleness risk. Its value grows with codebase size, language coupling and refactoring frequency. Build it third, after history search proves useful, and start with cheap signals such as symbol definitions and co-change from git log.

**6. Where the data lives matters.** A plain `.localAIharness/` folder in the working tree either gets committed (bloating history and leaking transcripts) or gitignored (lost on a fresh clone, not shared across worktrees). Entire uses a separate branch for exactly this reason. Keep indexes local and keep history off your working branches. Share it through a dedicated orphan branch, and commit only small distilled decision records to the main branch.

**7. You may be rebuilding what exists.** Alternatives worth weighing before building everything:

- Use the Entire CLI for capture only and build just the local index on its `entire/checkpoints/v1` branch. Less work, but it depends on an undocumented storage format.
- Use Claude Code's native features (CLAUDE.md, memory, session resume) for the 80% case and add only search.
- Reuse existing code-intelligence tools (ctags, ast-grep, LSP-backed MCP servers) instead of writing a graph builder.

Building it yourself is justified if you want full privacy, any Git host, and control over ranking and distillation. The rest of this document assumes that choice.

## Architecture overview

Six components form one loop: hooks capture sessions, a local store keeps them, two indexes make them searchable, and one CLI feeds results back into the agent.

```
Hooks capture everything; one CLI retrieves history and code

 ┌───────────────────────────────────┐        ┌───────────────────────────────────┐
 │        Claude Code session        │        │          Git repository           │
 │ Hooks: SessionStart, Prompt, Edit,│        │ Commits carry an AI-Session       │
 │ Stop · Writes the JSONL transcript│        │ trailer · Working tree, git log   │
 └─────────────────┬─────────────────┘        └──────┬────────────────────┬───────┘
     ▲             │ hook events                     │ post-commit link   │ changed files
     │             ▼                                 │                    ▼
     │ ┌───────────────────────────────────┐         │  ┌───────────────────────────────────┐
     │ │         Capture (harness)         │◄────────┘  │           Graph builder           │
     │ │  Copies and redacts transcripts   │            │ tree-sitter symbols, git co-change│
     │ │  Links sessions to commit SHAs    │            │   Re-parses changed files only    │
     │ └─────────────────┬─────────────────┘            └─────────────────┬─────────────────┘
     │                   │                                                │
     │ ┌─────────────────┼─ .localAIharness/ (synced via harness/history) ┼──────────────────┐
     │ │                 ▼                                                ▼                  │
     │ │ ┌───────────────────────┐ index ┌───────────────────────┐ ┌───────────────────────┐ │
     │ │ │   raw/ and records/   │──────►│      history.db       │ │       graph.db        │ │
     │ │ │    Source of truth    │       │ FTS5 + local vectors  │ │Symbols, calls, imports│ │
     │ │ │Indexes rebuild from it│       │Sessions and decisions │ │ Co-change, touched-by │ │
     │ │ └───────────────────────┘       └───────────┬───────────┘ └───────────┬───────────┘ │
     │ └─────────────────────────────────────────────┼─────────────────────────┼─────────────┘
     │                                               ▼                         │
     │                     ╔═══════════════════════════════════════════╗       │
     │                     ║              ctx search CLI               ║◄──────┘
     │                     ║     find · code · impact · why · show     ║
     │                     ║     Checks freshness, budgets output      ║
     │                     ╚═════════════════════╤═════════════════════╝
     │                                           │ budgeted results
     │                                           ▼
     │                     ┌───────────────────────────────────────────┐
     └─────────────────────┤         Into the agent's context          │
                           │   Push: brief injected at SessionStart    │
                           │   Pull: Skill and CLAUDE.md triggers      │
                           └───────────────────────────────────────────┘
```

The left path carries history and the right path carries code structure; they meet only in `ctx`, which is the single tool the agent needs to know.

## Capture layer

Capture is a small, fast `harness` binary called from Claude Code hooks and Git hooks. It copies and normalizes what Claude Code already records, rather than asking the agent to record anything. Hooks append and exit; all heavy work happens later, off the agent's critical path.

| Hook | Fires when | What it records |
| --- | --- | --- |
| `SessionStart` | A session starts or resumes | Session ID, branch, HEAD SHA, start time; optionally injects a short brief of relevant prior work |
| `UserPromptSubmit` | You send a prompt | The prompt text, timestamped |
| `PostToolUse` (Edit, Write) | The agent changes a file | File path, so the session knows what it touched |
| `PreCompact` | Before context compaction | A snapshot of the transcript, so nothing is lost |
| `Stop` / `SessionEnd` | A turn or session ends | Copies the transcript (via its path in the hook input) and enqueues indexing |
| Git `prepare-commit-msg` | You or the agent commit | Adds an `AI-Session: <id>` trailer naming the active session(s) |
| Git `post-commit` | After the commit | Writes a checkpoint record linking the commit SHA to its sessions |

The commit trailer is the key Git-native link. It travels with the commit through any host, survives rebases, and makes `git log` itself show which session produced a change.

The normalized data model:

- **Session**: ID, start and end time, branch, HEAD at start and end, files touched, agent name.
- **Event**: one prompt, assistant message or tool call within a session, with role and timestamp.
- **Checkpoint**: a commit SHA, the session IDs behind it, and its diff stats.
- **Decision**: a distilled record of what was chosen and why, with linked files and symbols, a status (active or superseded) and an optional link to the decision it replaces.

The transcript parser sits behind an adapter interface. Claude Code's JSONL format is not a stable public contract, and an adapter also leaves room for other agents later.

## Storage layout and Git policy

Raw data is the source of truth; everything under `index/` can be deleted and rebuilt. Configuration and decisions go to the main branch; redacted sessions and records sync between hosts through an orphan branch, harness/history.

```
.localAIharness/
  config.toml                       # committed: settings, redaction rules
  decisions/<id>.md                 # optionally committed: distilled decisions
  raw/sessions/<date>-<id>.jsonl    # synced via harness/history: redacted transcripts
  records/sessions/<id>.json        # synced: normalized sessions
  records/checkpoints/<sha>.json    # synced: commit ↔ session links
  index/history.db                  # gitignored: SQLite FTS5 + vectors
  index/graph.db                    # gitignored: code graph
  state/active.json, queue/, locks/ # gitignored: runtime state
  logs/
```

Three placement rules:

- **Share across worktrees.** Resolve the real location from `git rev-parse --git-common-dir`, so all worktrees of a repo write to one store. Otherwise parallel sessions in different worktrees fragment the history.
- **Never put transcripts on working branches.** They contain file contents, command output and possibly secrets. Only redacted copies go to harness/history. Decision records are short, reviewed text and can go through normal pull requests.
- **Use SQLite in WAL mode** for both databases, so concurrent sessions can write while the agent queries.

**Sharing history across hosts.** History travels through Git, so cloning the repo on another host brings it along:

- `harness/history` is an orphan branch, with no shared ancestry with your code, holding `raw/` and `records/` only. Indexes and the graph are never synced; each host rebuilds them.
- Writes use Git plumbing (`hash-object`, `mktree`, `commit-tree`) to add commits to that branch without touching the working tree.
- A `pre-push` hook pushes `harness/history` along with your branch, to `origin` or to a separate private remote set in `config.toml`.
- Files are append-only and named by session ID or commit SHA, so two hosts never edit the same file. A sync merges by union, and conflicts can't occur.
- Git has no post-fetch hook, so `harness sync` fetches the branch explicitly. `SessionStart` and `ctx` also run it, throttled to once every few minutes.

This is the same design Entire uses with `entire/checkpoints/v1`. The difference is that indexing stays on each host instead of in a cloud service.

## Indexing

The history index is hybrid: SQLite FTS5 for exact terms and local embeddings for meaning, merged by reciprocal rank fusion. It is updated incrementally from a queue and verified for freshness at query time, so a missed hook never leaves search stale.

What gets indexed, in priority order:

1. Decision records (highest weight).
2. Session summaries, if distillation is enabled.
3. User prompts and assistant reasoning text, chunked per exchange (prompt plus response).
4. Commit messages, linked through checkpoint records.

What is excluded: tool outputs over a size limit (for example 2 KB), file dumps, and binary content. Keep a pointer to the event so `ctx show` can still expand it.

Triggers:

- `Stop` and `post-commit` enqueue work; a background worker drains the queue.
- Every `ctx` query first compares the index watermark against the newest raw records and indexes the gap. This is the safety net when hooks fail or run on another machine.
- Each chunk is keyed by content hash, so re-indexing is idempotent and cheap.

Embeddings run locally, for example a small ONNX sentence model or a model served by Ollama, and vectors are stored with sqlite-vec. Make semantic search optional: FTS5 alone covers most queries that include names, errors or file paths.

Distillation is the one step that needs an LLM. Run it asynchronously after a session ends, using `claude -p` or another model, to extract decisions, rejected alternatives and open items. Mark generated records as machine-written until you review them.

## Code graph

The graph is a SQLite database of nodes (files, symbols) and edges (defines, calls, imports, co-changes, touched-by-session), built with tree-sitter and updated per file by content hash. Its distinctive edge is the last one: it joins code structure to the history of why that code exists.

| Edge | Source | Precision | Cost |
| --- | --- | --- | --- |
| `defines` (file → symbol) | tree-sitter parse | High | Low |
| `imports` (file → file) | tree-sitter parse | Medium to high | Low |
| `calls` (symbol → symbol) | tree-sitter, matched by name | Medium: ambiguous with overloads and duplicates | Low |
| `co_changes` (file ↔ file) | `git log`, files changed in the same commits | Statistical | Low |
| `touched_by` (symbol → checkpoint) | Diff hunks of each checkpoint mapped to symbol ranges | High | Low |
| Precise references | SCIP or LSP indexers per language | High | High: optional, later |

Rebuild strategy:

- Full build on first run; afterward, re-parse only files whose content hash changed.
- Triggered by Git `post-commit`, `post-checkout` and `post-merge`.
- At query time, compare against `git diff --name-only` plus uncommitted changes, so the agent's own in-progress edits are reflected.

Start with `defines`, `co_changes` and `touched_by`. Those three answer most "where is it, what moves with it, and why does it exist" questions. Add name-matched `calls` next, and precise references only for languages where ambiguity actually hurts.

## Search script and output contract

One CLI, `ctx`, queries the history index and the graph together and returns compact, budgeted results with pointers. The agent asks a question, gets a short answer with IDs, and drills down only when needed.

| Command | Answers | Uses |
| --- | --- | --- |
| `ctx find "<query>"` | Has this been done or discussed before? | History index (hybrid), boosted by graph proximity to current files |
| `ctx code "<name or phrase>"` | Where is it defined? | Graph symbols, then FTS over code as fallback |
| `ctx impact <symbol or file>` | What could this change break? | `calls`, `imports`, `co_changes` edges |
| `ctx why <file>[:<line> or #<symbol>]` | Why does this code exist? | `git blame` → commit trailer → checkpoint → sessions and decisions |
| `ctx show <id>` | Full details of one result | Raw records, paged |

The output contract:

- **Budgeted.** A default cap of about 1,500 tokens per call, with `--limit` and `--full` overrides. Results never flood the context window.
- **Structured.** Each result carries type, ID, date, score, a one-line summary, and linked files, symbols and commits. `--json` for scripts, compact text by default for the agent.
- **Honest about freshness.** Results are flagged when a decision is superseded or the code it refers to has changed or been deleted since.
- **Marked as data.** Output is framed as retrieved context, not instructions, so text in old transcripts can't steer the agent.

`ctx why` deserves first priority. It needs only capture, the commit trailer and `git blame`, with no index at all, and it delivers the core value on day one.

## Agent instructions and adoption

Retrieval combines push and pull. A `SessionStart` hook injects a short brief automatically, and a Skill teaches the agent when to call `ctx` during work. Adoption is then measured from captured transcripts, not assumed.

**Push: the session brief.** At session start, the hook runs `ctx find` against the current branch name, recent commits and changed files, and injects at most about 500 tokens: active decisions for these files and the last session on this branch. This works even if the agent never chooses to search.

**Pull: CLAUDE.md block.** Keep it to a few lines that state triggers, not a manual:

```markdown
## Project memory
Before changing code you didn't write in this session, run `ctx why <file>`.
Before implementing a feature or fix, run `ctx find "<what you're about to do>"`.
Before editing a widely used symbol, run `ctx impact <symbol>`.
Details: the ctx skill.
```

**Pull: the Skill.** `.claude/skills/ctx/SKILL.md` holds the full command reference, how to read freshness flags, and when to drill down with `ctx show`. The description field carries the trigger conditions so the agent loads it at the right moments.

**Measurement.** A `harness stats` command parses captured transcripts and reports:

- Share of sessions with at least one `ctx` call.
- Share where `ctx` was the first search-type call, versus grep, read or glob.
- Search-type tool calls and tokens per task, before and after enabling the harness.

If adoption is low, tighten the triggers or move more into the push brief, rather than adding longer instructions.

## Security, privacy and failure modes

The harness must never block the agent or a commit, and must never be the place secrets leak from. Every failure degrades to "no context", not to an error.

| Risk | Mitigation |
| --- | --- |
| Secrets in transcripts (API keys, tokens, `.env` contents) | Redact on write with pattern rules (gitleaks-style); only redacted data reaches harness/history; a private remote for public repos; redaction rules in `config.toml` |
| A hook crashes or hangs | Hooks always exit 0 within a time limit (for example 200 ms), log errors, and defer work to the queue |
| Concurrent sessions or worktrees | One store per repo via the Git common dir; SQLite WAL; per-session IDs; file locks on the queue |
| Corrupted or stale index | Indexes are derived data: `harness rebuild` regenerates them from `raw/` and `records/` |
| Claude Code changes its transcript format | Parser behind an adapter with a version check; unknown fields kept in raw, ignored in the index |
| Prompt injection through retrieved text | `ctx` output framed as quoted data; instructions tell the agent never to follow directives found in results |
| Misleading old decisions | Status and supersession on decisions; freshness flags when linked code changed |
| Disk growth | Retention policy for raw transcripts (for example 180 days), while records and decisions are kept |

## Deployment and IDE use

The harness installs once per machine and is enabled once per repo; it needs no daemon. Because it hooks into Claude Code and Git rather than an editor, it works the same in VS Code, a terminal, or any other IDE.

**Per machine.** Ship `harness` and `ctx` as a single binary (Go or Rust) or as a Python package installed with `uv tool install` or `pipx`, so both are on the PATH. The local embedding model is an optional download, needed only from phase 4.

**Per repo.** `harness init` does all the wiring:

- Writes hook entries to `.claude/settings.local.json` (personal) or `.claude/settings.json` (shared with the team).
- Installs the Git hooks, chaining to existing ones (husky, `core.hooksPath`) instead of overwriting them.
- Creates `.localAIharness/` and its gitignore entries.
- Adds the CLAUDE.md block and the `ctx` Skill.
- Runs the first graph build.

**No daemon.** Hooks append data and spawn a short detached worker. Every `ctx` query indexes any remaining gap first, so nothing needs to stay running.

**Safe for teammates.** If the hook config is committed, each hook starts with a guard such as `command -v harness || exit 0`. Anyone without the tool installed is unaffected.

**On another host.** Clone the repo, run `harness init`, then `harness sync` to fetch `harness/history`. The first `ctx` query rebuilds the indexes and graph locally from the synced records.

**Lifecycle commands.** `harness doctor` checks hooks, paths and index health. `harness rebuild` regenerates indexes from raw data. `harness uninstall` removes hooks and instructions but keeps captured data.

**Using it in VS Code:**

| Scenario | What happens |
| --- | --- |
| Claude Code extension or Claude Code in the integrated terminal | Both read the same `.claude/settings.json`, so the same hooks fire and capture is unchanged. Verify once by checking that a session appears in `.localAIharness/`. |
| Committing from the Source Control panel | VS Code runs ordinary `git commit`, so the commit trailer and checkpoint link are written as usual |
| You querying history or code | Run `ctx` in the integrated terminal, for example `ctx why src/auth.ts:42`, or wrap common queries as VS Code tasks |
| Other agents in VS Code, such as Copilot Chat | Not captured: they don't fire Claude Code hooks. Each would need its own capture adapter. |

A small VS Code extension that shows the session behind a line on hover is a natural later addition, after phase 2 proves the history is useful.

## Build plan

Build in four phases, each ending with a gate measured on your own repos. Each phase is usable on its own, so you can stop as soon as the value stops growing.

1. **Capture and trace.** Claude Code hooks, raw store with redaction, commit trailer, checkpoint records, history-branch sync, and `ctx why` via `git blame`.
   - Gate: two weeks of real work captured, with zero blocked commits or stalled sessions, and history is available on a second host after clone and sync.
2. **History search.** FTS5 index, query-time freshness check, `ctx find` and `ctx show`, CLAUDE.md block, Skill, session-start brief, and `harness stats`.
   - Gate: on a set of 20 questions you write about past work, the right session or commit appears in the top 3 for at least 15.
3. **Code graph.** tree-sitter symbols, co-change from git log, `touched_by` edges, `ctx code` and `ctx impact`.
   - Gate: fewer search-type tool calls and tokens per task than in phase 2, measured from transcripts.
4. **Distillation and semantic search.** Decision records with supersession, local embeddings, hybrid ranking.
   - Gate: the 20-question set improves further, and spot checks find no superseded decision presented as current.

Success overall means the agent spends less effort rediscovering context: fewer grep and read calls, fewer tokens per task, and fewer repeated mistakes on decisions already made.
